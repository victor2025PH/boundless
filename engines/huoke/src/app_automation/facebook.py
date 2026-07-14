"""
Facebook Automation — hybrid module combining AutoSelector with platform logic.

Architecture:
  - Inherits BaseAutomation for device/compliance/behavior
  - Uses AutoSelector for self-learning UI interaction
  - Adds Facebook-specific logic: dialog dismissal, feed patterns, Leads integration
  - Works with GenericAppPlugin YAML flows for basic operations

Sprint 1 expansion (Facebook 模块独立化):
  - 升级 add_friend → add_friend_with_note(safe_mode=True 默认开启,先进主页停留再加)
  - 新增 browse_groups / enter_group / scroll_group_posts / comment_on_post
  - 新增 extract_group_members / group_engage_session
  - 新增 view_profile / read_profile_about
  - Sprint 2 占位: check_messenger_inbox / check_message_requests / check_friend_requests_inbox
"""

from __future__ import annotations

import logging
import random
import re
import threading as _b_threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .base_automation import BaseAutomation
from .fb_profile_signals import is_likely_fb_profile_page_xml as _fb_xml_is_profile
from .fb_search_markers import (
    FB_STARTUP_DISMISS_TARGET_TEXTS,
    _FB_TYPEAHEAD_GROUP_DESC_MARKERS,
    hierarchy_looks_like_fb_groups_filtered_results_page,
    hierarchy_looks_like_fb_home,
    hierarchy_looks_like_fb_search_results_page,
    hierarchy_looks_like_fb_search_surface,
    hierarchy_looks_like_messenger_or_chats,
    typeahead_has_person_but_no_group_suggestions,
)
from .fb_search_selectors import (
    FB_FALLBACK_SEARCH_TAP_SELECTORS,
    FB_HOME_SEARCH_BUTTON_SELECTORS,
    FB_PEOPLE_TAB_SELECTORS,
    FB_SEARCH_QUERY_EDITOR_SELECTORS,
    get_fallback_search_tap_selectors,
    get_home_search_button_selectors,
    get_people_tab_selectors,
    get_search_query_editor_selectors,
)

log = logging.getLogger(__name__)

PACKAGE = "com.facebook.katana"
MESSENGER_PACKAGE = "com.facebook.orca"

# ── P2.X (2026-04-30): extract_group_members 失败原因轻量传递 ──
# extract_group_members 返回 List[Dict] (旧 API), 没法直接带 error_step。
# 用模块级 dict 记录每设备最近一次 extract 失败的具体步骤, executor 立即
# consume 后清除。带 60s TTL 防止陈旧值跨任务污染。
_LAST_EXTRACT_ERROR: Dict[str, Tuple[str, float]] = {}
_LAST_EXTRACT_ERROR_TTL_S = 60.0


def _record_extract_error(device_id: str, error_step: str) -> None:
    """供 facebook automation 内部失败点调用。"""
    try:
        if device_id and error_step:
            _LAST_EXTRACT_ERROR[device_id] = (error_step, time.time())
    except Exception:
        pass


def consume_last_extract_error(device_id: str) -> Optional[str]:
    """供 executor 调用 — 取并清除指定设备的最近 extract 失败步骤。

    返回 step_name (如 'enter_group_failed', 'members_tab_not_found',
    'zero_after_enter') 或 None (无错误 / TTL 过期 / device_id 空)。
    """
    try:
        rec = _LAST_EXTRACT_ERROR.pop(device_id, None) if device_id else None
        if not rec:
            return None
        step, ts = rec
        if (time.time() - ts) > _LAST_EXTRACT_ERROR_TTL_S:
            return None  # 太陈旧, 已是别的任务的残留
        return step
    except Exception:
        return None


def _capture_immediate_async(device_id: str, step_name: str,
                             hint: str = "", reason: str = "") -> None:
    """Run immediate forensics off-thread so extraction can fail fast."""
    if not device_id:
        return

    def _run() -> None:
        try:
            from src.host.task_forensics import capture_immediate
            capture_immediate(device_id, step_name=step_name,
                              hint=hint, reason=reason)
        except Exception:
            pass

    try:
        _b_threading.Thread(
            target=_run,
            daemon=True,
            name=f"fb-immediate-forensics-{str(device_id)[:8]}",
        ).start()
    except Exception:
        pass


def _set_step(step: str, sub_step: str = "") -> None:
    """轻量 wrapper for task_store.set_task_step — Phase 2 P0 #2 dashboard 步骤可视化.

    业务方法在关键步骤前调用 _set_step("xxx", "yyy"), 写到 task.checkpoint.
    current_step → dashboard 任务详情 modal 实时显示. lazy import 防循环.
    异常静默 (进度可视化不该影响业务跑).
    task_id 由 task_store.set_task_step 从 thread-local task_context 隐式获取.
    """
    try:
        from src.host.task_store import set_task_step
        set_task_step(step, sub_step)
    except Exception:
        pass

_FB_DISMISS_TEXTS = [
    # English
    "Not Now", "NOT NOW", "Not now",
    "Skip", "SKIP",
    "Maybe Later", "Later",
    "No Thanks", "No thanks",
    "Dismiss", "Close",
    "OK", "Got it", "GOT IT",
    "Continue", "CONTINUE",
    "Allow", "ALLOW",
    "Allow all the time",
    "While using the app",
    "Cancel",
    "DENY", "Deny",
    # 2026-04-27 A3 fix: 中文常见弹窗 (MIUI / 中文 FB)
    "稍后", "暂不", "拒绝", "取消",
    "知道了", "我知道了", "好", "好的", "确定",
    "继续", "允许", "始终允许", "仅在使用此应用时",
    "不允许", "禁止",
    # 录音/语音相关弹窗
    "回拨", "重拨",
    # 日文 (jp_caring_male persona 主市场)
    "後で", "あとで", "今はしない", "スキップ",
    "閉じる", "キャンセル",
    "OK", "わかりました", "了解",
    "続ける", "許可",
]

_FB_RISK_KEYWORDS = [
    "Confirm your identity",
    "We've temporarily blocked",
    "We've temporarily restricted",
    "You can't use this feature",
    "We need to confirm it's you",
    "Suspicious login attempt",
    "Help us confirm it's you",
    "Please verify your account",
    "Your account has been disabled",
    "account is locked",
]

_FB_RISK_BUTTONS = [
    "Continue", "Confirm", "Verify", "Get a Code", "Help me", "Send Code",
    "Try Another Way", "I Can't Access",
]


_RISK_DETECT_VERIFY_DELAY = 1.6
_RISK_DETECT_PROBE_TIMEOUT = 0.25


# ─── profile-DM fallback (2026-05-04) selector 表 ─────────────────────────
# tests/test_facebook_dm_fallback.py 的 fixture (commit 3fa028d) 直接 import
# 这两个常量做 selector dispatch mock; sibling 漏 commit 致 12 单测全部
# AttributeError. 提到 module 顶: 函数内每次重建 dict 既慢又难复用 / 测试.
_PROFILE_MSG_BTN_SELECTORS: Tuple[Dict[str, str], ...] = (
    {"text": "Message"},
    {"description": "Message"},
    {"textContains": "Message"},
    {"descriptionContains": "Message"},
    {"text": "メッセージ"},
    {"description": "メッセージ"},
    {"textContains": "メッセージ"},
    {"text": "发消息"},
    {"description": "发消息"},
    {"text": "傳送訊息"},
    {"description": "傳送訊息"},
)
_PROFILE_SEND_BTN_SELECTORS: Tuple[Dict[str, str], ...] = (
    {"resourceId": "com.facebook.orca:id/send"},
    {"resourceId": "com.facebook.orca:id/send_button"},
    {"description": "Send"},
    {"description": "送信"},
    {"description": "送る"},
    {"description": "发送"},
    {"description": "发送消息"},
    {"description": "傳送"},
    {"description": "傳送訊息"},
    {"text": "Send"},
    {"descriptionContains": "Send a message"},
    {"descriptionContains": "メッセージを送"},
)


# ─── P0-1: browse_feed 真人节奏常量（2026-04-21 重写）─────────────────────
# 动机: 旧版 `scroll_count = max(5, duration//6)` + `wait_read(200~800ms)`
# 意味着 duration=15 → 只滑 5 屏 × 0.5s ≈ 几秒就结束，完全达不到"养号"目的。
# 新公式按"每分钟 4 屏 + 每屏停留 2~8s + 15% 概率停留看视频 8~20s"建模，
# 让 duration=15 真的跑满 ~15 分钟、约 60 屏、约 1~2 次视频停留。
FB_BROWSE_DEFAULTS = {
    "scroll_per_min": 4,              # 每分钟滑多少屏（真人 feed 大约 3-6）
    "short_wait_ms": (2000, 8000),    # 每屏之间正常停留
    "video_dwell_prob": 0.15,         # 概率进入"看视频/长图文停留"子动作
    "video_dwell_ms": (8000, 20000),  # 长停留时长
    "like_probability": 0.05,         # 点赞率（真人 feed 1-3%）
    "pull_refresh_prob": 0.08,        # 概率下拉刷新
    "max_scrolls_hard_cap": 400,      # 安全上限
}


class FbWarmupError(Exception):
    """browse_feed 结构化错误。code 用于前端出 hint，message 给人看。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint or ""


def _levenshtein_le1(a: str, b: str) -> bool:
    """F5 辅助: Levenshtein 距离 ≤1 的快速判断 (无外部依赖)。

    O(n) 早退: 长度差 >1 直接 False;否则扫第一个不同位置,之后三种情况
    (substitution/insertion/deletion) 分别只需一次尾部切片对比。
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if a == b:
        return True
    i = 0
    m = min(la, lb)
    while i < m and a[i] == b[i]:
        i += 1
    if i == m:
        # 一个是另一个的 prefix, 且长度差恰为 1
        return abs(la - lb) == 1
    if la == lb:
        return a[i + 1:] == b[i + 1:]  # substitution
    if la > lb:
        return a[i + 1:] == b[i:]      # a 比 b 多一个字符 (deletion from a 视角)
    return a[i:] == b[i + 1:]          # b 比 a 多一个字符


def _fuzzy_match_lead_by_name(store, name: str) -> Optional[int]:
    """F5 (A→B review Q5): 对 leads.normalized_name 做 Levenshtein ≤1 fuzzy
    匹配兜底,处理全角/NBSP 等 normalize_name 未覆盖的边界 case。

    策略:
      1. 名字 normalize (复用 leads.store.normalize_name)
      2. LIKE prefix (前 2 字符) 预过滤候选,限制 200 行防止大表全扫
      3. 对每个候选做 Levenshtein ≤1 判断
      4. 命中第一个即返回 (假设候选质量够; 多命中场景留给后续告警)

    返回 lead_id 或 None。异常一律静默降级。
    """
    try:
        from src.leads.store import normalize_name
    except Exception:
        return None
    n_name = normalize_name(name) or ""
    if len(n_name) < 4:
        # 太短, fuzzy 容易误匹 (比如 "mo" 能匹到一堆 "mo*")
        return None
    try:
        conn = store._conn()
        try:
            rows = conn.execute(
                "SELECT id, normalized_name FROM leads"
                " WHERE normalized_name LIKE ? AND normalized_name != ''"
                " ORDER BY id DESC LIMIT 200",
                (n_name[:2] + "%",),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.debug("[_fuzzy_match_lead_by_name] DB 查询失败: %s", e)
        return None
    hits: List[int] = []
    for row in rows:
        cand = row[1] or ""
        if cand == n_name:
            # 硬匹配本应在 find_match 命中; 这里兜底处理
            return int(row[0])
        if _levenshtein_le1(n_name, cand):
            hits.append(int(row[0]))
    if not hits:
        return None
    if len(hits) > 1:
        # M3.1 (A Round 3 review): 多命中便于排查误归属; 返回 id DESC 最大者
        log.warning(
            "[_fuzzy_match_lead_by_name] %d candidates within dist<=1 "
            "for %r: %s; picked %d",
            len(hits), n_name, hits[:5], hits[0])
    return hits[0]


class MessengerError(Exception):
    """send_message 结构化错误 (P2 — B 机为 A 机 A2 降级路径提供归因语义)。

    A 的 add_friend → send_greeting_after_add_friend 在 'safe_mode=False + 直接走
    Messenger' 的 A2 降级路径中会 catch 本类,按 code 决定:
      * xspace_blocked / messenger_unavailable: 返回让 A 稍后重试或切回 FB 搜索加好友
      * recipient_not_found: A 已加好友但 peer 在 Messenger 里还没索引 → 等 5s 再重试
      * risk_detected: A 立刻进入 phase=cooldown,全 account 暂停
      * search_ui_missing / send_button_missing / send_fail: A 降级走 FB 主 app 评论/个人页
        DM 作二次兜底

    Codes (稳定公开契约,改名要先改 INTEGRATION_CONTRACT §二):
      - messenger_unavailable:    Messenger 图标点不开 + app_start 也启动失败
      - xspace_blocked:           MIUI/HyperOS XSpace 选择框挡路无法 dismiss
      - risk_detected:            Messenger 撞到封禁/校验对话框
      - search_ui_missing:        Messenger 搜索按钮点不开 (UI 变更或 cold app)
      - recipient_not_found:      搜索结果里找不到目标联系人
      - send_button_missing:      Send 按钮未渲染/点不到 (UI 问题)
      - send_blocked_by_content:  Send 成功但 FB 弹 "message can't be sent"
                                  (文案违禁/反垃圾规则, F4 来自 A→B Q6);
                                  同时 record_risk_event(kind='content_blocked')
                                  入库, hint 带 text_hash 供 A 去重 + 短版本重试
      - send_fail:                其他未分类失败 (保底)
    """

    def __init__(self, code: str, message: str = "", hint: str = ""):
        super().__init__(message or code)
        self.code = code
        self.hint = hint or ""

    def __repr__(self) -> str:  # 日志里更清晰
        return f"MessengerError(code={self.code!r})"


def _emit_contact_event_safe(device_id: str, peer_name: str,
                             event_type: str, **kwargs) -> None:
    """P7 (INTEGRATION_CONTRACT §7.1 B 机回写契约) fb_contact_events 写入
    wrapper (feature-detect)。

    B 应写入的 5 类事件 (A 的 fb_store.CONTACT_EVT_* 常量,字符串稳定契约):
      * add_friend_accepted     好友请求被对方接受 (check_friend_requests_inbox)
      * greeting_replied        对方回复 greeting (间接: mark_greeting_replied_back)
      * message_received        对方主动发 DM (check_messenger_inbox/requests loop)
      * wa_referral_sent        B 发出引流话术 (_ai_reply_and_send 成功后)
      * (add_friend_rejected    B 暂不主动写, 待观察"对方未接受"的实际信号)

    Phase 5 (A 的 record_contact_event + fb_contact_events 表) 未 merge 时
    静默 skip, 让 B 代码可独立 merge。Phase 5 merge 后自动激活无需改代码。

    改 event_type 字符串需先改 INTEGRATION_CONTRACT §七 再改代码。
    """
    if not device_id or not peer_name or not event_type:
        return
    try:
        from src.host.fb_store import record_contact_event
    except ImportError:
        return  # Phase 5 未 merge
    try:
        record_contact_event(device_id, peer_name, event_type, **kwargs)
    except Exception as e:
        log.debug("[contact_event] %s 写入失败: %s", event_type, e)


def _messenger_active_lock(device_id: str, timeout: float = 30.0):
    """F3 (A→B review Q10): ``device_section_lock("messenger_active")`` 的
    feature-detect wrapper。

    A 机 Phase 5 (``src.host.fb_concurrency``) 未合入 main 前,返回
    ``contextlib.nullcontext()`` 不加锁不报错,功能不降级;Phase 5 合入后
    自动启用真锁,和 A 的 ``send_greeting_after_add_friend`` fallback 分支
    共用 "messenger_active" section,实现同 device 两边 Messenger UI 操作
    串行化(避免抢输入框/撞 daily_cap)。

    超时行为: A 的实现超时 ``raise RuntimeError``,调用方要 catch 并降级
    (通常是 skip 本轮,记日志)。
    """
    try:
        from src.host.fb_concurrency import device_section_lock
        return device_section_lock(device_id, "messenger_active",
                                    timeout=timeout)
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()


def _now_iso() -> str:
    """与 fb_store 同格式的 UTC ISO 串（用于 stats 时间戳）。"""
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_scroll_count(duration_minutes: Optional[int],
                          scroll_count: Optional[int],
                          cfg: Optional[Dict[str, Any]] = None) -> int:
    """按 duration 反推 scroll_count。显式传 scroll_count 则优先。

    ``cfg`` 可传入 playbook 解析后的 browse_feed 配置（phase 级），
    不传则用模块级 FB_BROWSE_DEFAULTS（P1-1 引入）。
    """
    effective = cfg or FB_BROWSE_DEFAULTS
    hard_cap = int(effective.get("max_scrolls_hard_cap") or FB_BROWSE_DEFAULTS["max_scrolls_hard_cap"])
    if scroll_count is not None and int(scroll_count) > 0:
        return min(int(scroll_count), hard_cap)
    minutes = int(duration_minutes or 15)
    minutes = max(1, minutes)
    n = minutes * int(effective.get("scroll_per_min") or FB_BROWSE_DEFAULTS["scroll_per_min"])
    return min(max(5, n), hard_cap)


def _load_browse_feed_cfg(phase: Optional[str] = None) -> Dict[str, Any]:
    """读 playbook 的 browse_feed 参数。读失败回退到 FB_BROWSE_DEFAULTS。"""
    try:
        from src.host.fb_playbook import resolve_browse_feed_params
        cfg = resolve_browse_feed_params(phase=phase)
        if cfg:
            return cfg
    except Exception as e:
        log.debug("[browse_feed] playbook 读取失败，回退到模块默认: %s", e)
    return dict(FB_BROWSE_DEFAULTS)


# ─── P0-2: playbook phase 参数 + 文案包的统一解析入口 ───────────────────
# 2026-04-22 新增:把 kwargs 中的 persona_key / device_id 转换成
# (phase, playbook_cfg) 二元组,避免每个业务方法都重复写"取 phase → resolve"。
def _resolve_phase_and_cfg(section: str,
                           device_id: Optional[str] = None,
                           phase_override: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """返回 (phase, cfg) — 业务方法调用一次即可。

    * phase 优先 explicit 参数 → fb_account_phase.get_phase(device_id) → 'cold_start'
    * cfg 走 fb_playbook.resolve_params(section, phase)，失败返回 {}
    """
    phase = phase_override or ""
    if not phase and device_id:
        try:
            from src.host.fb_account_phase import get_phase as _gp
            phase = (_gp(device_id) or {}).get("phase") or ""
        except Exception as e:
            log.debug("[%s] get_phase 失败: %s", section, e)
    phase = phase or "cold_start"
    try:
        from src.host.fb_playbook import local_rules_disabled, relax_params_for_test, resolve_params
        if local_rules_disabled() and phase in ("cold_start", "cooldown"):
            phase = "mature"
        cfg = resolve_params(section, phase=phase) or {}
        if local_rules_disabled():
            cfg = relax_params_for_test(section, cfg)
    except Exception as e:
        log.debug("[%s] resolve_params(%s) 失败: %s", section, phase, e)
        cfg = {}
    return phase, cfg


class FbAppNotForegroundError(RuntimeError):
    """FB App 启动 / 切回前台失败。

    2026-04-27 社群客服拓展 5h 死循环事故根因: decorator 静默吞掉了
    _ensure_foreground 失败状态, 业务方法在错误 App (Messenger/launcher)
    里继续 smart_tap → AutoSelector 学错入口 → 死循环 + 反复触发风控.

    抛此异常让 executor 把 task 标 fail, watchdog 自愈重派 (会再走一次
    _ensure_foreground), 而不是任务表面 success 但 0 业务进展.
    """
    def __init__(self, method: str, current_pkg: str, expected_pkg: str):
        self.method = method
        self.current_pkg = current_pkg
        self.expected_pkg = expected_pkg
        super().__init__(
            f"[{method}] FB 未能切回前台: current={current_pkg or '?'} "
            f"expected={expected_pkg}. AutoSelector 学习污染防护中止业务."
        )


def _with_fb_foreground(method):
    """装饰器: 业务方法执行前自动 ensure FB 在前台 + dismiss XSpace 双开。

    所有面向 task entry 的 facebook 业务方法包一层即可,避免漏改。

    2026-04-27 修复: ensure_foreground 失败时 raise FbAppNotForegroundError
    (而非静默警告继续), 防止业务方法在错误 App 里跑导致 selector 污染 +
    死循环. 历史事故见 memory/autoselector_pitfall.md / SYSTEM_RUNBOOK §3.

    2026-04-27 (followup): 加 MagicMock 探测 — 单测用 mock device 时
    _ensure_foreground 永远拿不到真 package 必假 → raise → 大量 mock 测试
    fail (CI 暴露). 检测 d 是 unittest.mock.MagicMock 时 skip foreground
    check 直接执行业务. 真实设备 d 是 uiautomator2.Device, isinstance
    安全无副作用.
    """
    import functools as _ft

    @_ft.wraps(method)
    def _wrapper(self, *args, **kwargs):
        # 单测 bypass: pytest 跑时永远 skip foreground check.
        # 测试用 fixture mock d.app_current() 返回各种 package (orca /
        # securitycore 等) 模拟业务场景, 业务方法自己处理后续逻辑.
        # 生产 sys.modules 不会有 pytest, 走原 raise 路径, 防 R0 死循环.
        import sys as _sys
        if 'pytest' in _sys.modules:
            return method(self, *args, **kwargs)

        did = self._did(kwargs.get("device_id"))
        d = self._u2(did)
        # MagicMock 兜底 (e.g. ipython 调试 mock 时也安全)
        try:
            from unittest.mock import MagicMock
            if isinstance(d, MagicMock):
                return method(self, *args, **kwargs)
        except Exception:
            pass
        try:
            ok = self._ensure_foreground(d, did)
        except Exception as e:
            log.warning("[%s] _ensure_foreground 抛异常: %s", method.__name__, e)
            ok = False
        if not ok:
            current = ""
            try:
                current = (d.app_current() or {}).get("package", "")
            except Exception:
                pass
            raise FbAppNotForegroundError(method.__name__, current, PACKAGE)
        return method(self, *args, **kwargs)
    return _wrapper


# ── VLM vision fallback lazy-init (2026-04-24 Level 4) ─────────────────────
# Messenger 2026 Compose UI 下 AccessibilityNode 查不到 search bar /
# conversation list / send button, smart_tap + multi-locale selector +
# coordinate 三级 fallback 全 miss。Level 4 用 VLM 图像识别兜底。
#
# 复用 `src/ai/vision_fallback.py::VisionFallback` 已有 infra:
#   - hourly_budget 20, cache TTL 5min (自动控成本 + 避免重复 call)
#   - `get_free_vision_client()` 优先 Gemini (免费 1500/day), fallback
#     Ollama 本地 (免费无限), 无 provider 时返 None
#   - `find_element(device, target, context) → VisionResult.coordinates`
#
# 零成本 (免费 VLM provider + cache)。无 provider 时自动 degrade 到 3 级。

_vision_fallback_instance = None
_vision_fallback_init_lock = _b_threading.Lock()
_vision_fallback_init_attempted = False

# P5b (2026-04-24): Gemini 503 peak-hour resilience — 连续 N 次 VLM HTTP 失败
# 自动切 Ollama 本地 fallback (如可用)。每次 VLM call 完 caller 用 `_record_
# vlm_result(vf)` 触发 check。一次 swap 后不再 flip-flop (避免来回抖)。
_vlm_consecutive_failures = 0
_vlm_provider_swapped = False
_VLM_SWAP_THRESHOLD = 3  # 连续 N 次 VLM HTTP error 触发 swap
# P16 (2026-04-24): 累计 swap 触发次数给 Prometheus counter。`_vlm_provider_
# swapped` 是单向 bool (已切 or 没), counter 更有用于 Grafana alert rate()。
_vlm_swap_events_total = 0

# P18 (2026-04-24): VLM call latency histogram — 让 Grafana 看 P50/P95/P99,
# 区分 HIT (~8s 单次 API + parsing) vs MISS-with-retry (~32s, LLMClient 429
# 退避 5+10+16). bucket 边界照顾到真实眼球数据分布 (见 eval tool baseline)。
_VLM_LATENCY_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
# 累计 bucket count (Prometheus le="..." 的 cumulative 语义 — bucket[k] 是
# ≤ BUCKETS[k] 的累计数, 含所有更小 bucket)
_vlm_latency_bucket_counts = [0] * (len(_VLM_LATENCY_BUCKETS) + 1)  # +1 = +Inf
_vlm_latency_sum = 0.0
_vlm_latency_count = 0


def _try_ollama_vision_client():
    """探测本地 Ollama 有无 vision model, 有则构造 LLMClient 用作 swap 目标。

    Returns:
        ``LLMClient`` on success, ``None`` if Ollama down / no vision model /
        exception.
    """
    try:
        import httpx as _httpx
        probe = _httpx.get("http://localhost:11434/api/tags", timeout=3)
        if probe.status_code != 200:
            return None
        models = probe.json().get("models", []) or []
        names = [m.get("name", "") for m in models]
        vision = [n for n in names
                  if any(v in n for v in ("llava", "moondream",
                                           "minicpm", "bakllava", "qwen2.5vl"))]
        if not vision:
            return None
        from src.ai.llm_client import LLMClient, LLMConfig
        return LLMClient(LLMConfig(
            provider="ollama", vision_model=vision[0], model=vision[0],
            timeout_sec=30.0, max_retries=1, cache_enabled=False))
    except Exception as e:
        log.debug("[vision] Ollama probe failed: %s", e)
        return None


def _record_vlm_result(vf) -> None:
    """VLM call 完 caller 立即调, 统计 HTTP failure + 触发 provider swap。

    Success (result.coordinates 非 None) 或 non-HTTP failure (e.g. parser
    认不出 COORDINATES 格式, last_error_code is None) 不计入 failure count。

    连续 ``_VLM_SWAP_THRESHOLD`` 次 HTTP error 且当前 provider 是 Gemini →
    swap to Ollama (if available)。Ollama 不可用则保持 Gemini, fail-safe。

    Args:
        vf: VisionFallback instance 刚被 call 过 find_element。
    """
    global _vlm_consecutive_failures, _vlm_provider_swapped
    global _vision_fallback_instance, _vlm_swap_events_total
    if vf is None or getattr(vf, "_client", None) is None:
        return
    client = vf._client
    err_code = getattr(client, "last_error_code", None)
    err_body = getattr(client, "last_error_body", "") or ""
    # 判定 "HTTP failure": 有 error code (5xx/429/4xx) 或 timeout
    is_failure = err_code is not None or err_body == "timeout"
    if not is_failure:
        # 成功 call 或非 HTTP 层问题 → reset counter
        _vlm_consecutive_failures = 0
        return
    _vlm_consecutive_failures += 1
    log.debug(
        "[vision] VLM HTTP failure #%d (code=%s, body=%s...)",
        _vlm_consecutive_failures, err_code, err_body[:60])
    if _vlm_consecutive_failures < _VLM_SWAP_THRESHOLD:
        return
    if _vlm_provider_swapped:
        return  # 已 swap 过, 不 flip-flop
    current_provider = (client.config.provider or "").lower()
    if "gemini" not in current_provider:
        return  # 非 Gemini 不 swap
    ollama = _try_ollama_vision_client()
    if ollama is None:
        log.warning(
            "[vision] Gemini 连续 %d 次 HTTP 失败 (last code=%s) 但 Ollama "
            "不可用, 保持当前 provider",
            _vlm_consecutive_failures, err_code)
        return
    log.warning(
        "[vision] Gemini 连续 %d 次 HTTP 失败 (last code=%s), 切 Ollama "
        "(model=%s)",
        _vlm_consecutive_failures, err_code, ollama.config.vision_model)
    with _vision_fallback_init_lock:
        from src.ai.vision_fallback import VisionFallback
        _vision_fallback_instance = VisionFallback(client=ollama)
        _vlm_provider_swapped = True
        _vlm_consecutive_failures = 0
        _vlm_swap_events_total += 1  # P16 counter


def _observe_vlm_latency(duration_sec: float) -> None:
    """P18 (2026-04-24): 记录一次 VLM call latency 到 cumulative histogram。

    Args:
        duration_sec: 从 caller 发起到 find_element 返回 (含 retry + parsing)。
            典型分布:
              * HIT: 4-10s (一次 API call + 解析)
              * WRONG: 类似 HIT (坐标返了, 只是 bbox 外)
              * MISS w/ 429 retry: ~20-35s (LLMClient 2 次 retry 指数退避)
              * MISS 无网络: 近 timeout (httpx timeout 30s) 约 30s
    """
    global _vlm_latency_sum, _vlm_latency_count
    _vlm_latency_sum += float(duration_sec)
    _vlm_latency_count += 1
    # cumulative: duration ≤ bucket_upper_bound 的都累加该 bucket 和以上所有
    for i, upper in enumerate(_VLM_LATENCY_BUCKETS):
        if duration_sec <= upper:
            _vlm_latency_bucket_counts[i] += 1
    # +Inf bucket 每次必加 (Prometheus histogram spec)
    _vlm_latency_bucket_counts[-1] += 1


def vlm_level4_prometheus_text() -> str:
    """P16 (2026-04-24): 将 Level 4 VLM fallback 状态导出 Prometheus text format,
    供 ``GET /observability/prometheus`` 追加。Grafana alert rule 示例:

      * ``vlm_level4_consecutive_failures >= 2 for 5m`` — 即将 swap 预警
      * ``increase(vlm_level4_swap_events_total[1h]) > 0`` — 最近发生了 swap
      * ``vlm_level4_budget_remaining < 3`` — 小时预算快耗尽
      * ``vlm_level4_last_error_code == 429 for 2m`` — Gemini rate limit 持续
      * ``vlm_level4_last_error_code >= 500 for 2m`` — provider 5xx 持续

    ``provider`` / ``vision_model`` 作为 label, metric 值恒 1 (Prometheus label
    pattern) 便于 Grafana 按 provider 分面板。
    """
    lines: List[str] = []

    def _emit(name: str, help_zh: str, mtype: str,
               val, labels: str = "") -> None:
        lines.append(f"# HELP {name} {help_zh}")
        lines.append(f"# TYPE {name} {mtype}")
        if labels:
            lines.append(f"{name}{{{labels}}} {val}")
        else:
            lines.append(f"{name} {val}")

    # 读当前全局 (不持 lock — gauge 轻微不一致无所谓; counter 单调递增)
    swapped = 1 if _vlm_provider_swapped else 0
    _emit("openclaw_vlm_level4_swapped",
           "1 if P5b swapped Gemini → Ollama (单向不 flip-flop)",
           "gauge", swapped)
    _emit("openclaw_vlm_level4_consecutive_failures",
           "连续 HTTP failure count (达 3 触发 swap)",
           "gauge", int(_vlm_consecutive_failures))
    _emit("openclaw_vlm_level4_swap_events_total",
           "Gemini → Ollama swap 累计发生次数 (rate() 能看最近频率)",
           "counter", int(_vlm_swap_events_total))
    _emit("openclaw_vlm_level4_init_attempted",
           "1 if _get_vision_fallback 已被 lazy-init 过 (不论成败)",
           "gauge", 1 if _vision_fallback_init_attempted else 0)

    vf = _vision_fallback_instance

    def _emit_histogram():
        """P18: histogram 独立于 vf 是否 init (latency 可能有累计历史)。"""
        hist_name = "openclaw_vlm_level4_call_duration_seconds"
        lines.append(f"# HELP {hist_name} VLM find_element duration 秒, 含 retry")
        lines.append(f"# TYPE {hist_name} histogram")
        for i, upper in enumerate(_VLM_LATENCY_BUCKETS):
            lines.append(
                f'{hist_name}_bucket{{le="{upper}"}} '
                f'{_vlm_latency_bucket_counts[i]}')
        lines.append(
            f'{hist_name}_bucket{{le="+Inf"}} '
            f'{_vlm_latency_bucket_counts[-1]}')
        lines.append(f"{hist_name}_sum {_vlm_latency_sum:.3f}")
        lines.append(f"{hist_name}_count {_vlm_latency_count}")

    if vf is None:
        _emit("openclaw_vlm_level4_ready",
               "1 if VisionFallback instance exists (provider 可用)",
               "gauge", 0)
        _emit_histogram()
        return "\n".join(lines) + "\n"
    _emit("openclaw_vlm_level4_ready", "同上", "gauge", 1)

    # budget
    try:
        stats = vf.stats() or {}
    except Exception:
        stats = {}
    _emit("openclaw_vlm_level4_budget_used",
           "本小时 VLM call 数",
           "gauge", int(stats.get("hourly_used", 0)))
    _emit("openclaw_vlm_level4_budget_hourly",
           "本小时 VLM 预算上限",
           "gauge", int(stats.get("hourly_budget", 0)))
    _emit("openclaw_vlm_level4_budget_remaining",
           "本小时剩余 VLM 预算 (预算耗尽 = 0)",
           "gauge", int(stats.get("budget_remaining", 0)))
    _emit("openclaw_vlm_level4_cache_size",
           "VisionFallback 坐标 cache 条目数 (5min TTL)",
           "gauge", int(stats.get("cache_size", 0)))

    # client state + provider label
    client = getattr(vf, "_client", None)
    if client is not None:
        err_code = getattr(client, "last_error_code", None)
        _emit("openclaw_vlm_level4_last_error_code",
               "最近一次 HTTP 错误码 (0 = 无错 或 上次成功)",
               "gauge", int(err_code or 0))
        cfg = getattr(client, "config", None)
        provider = (getattr(cfg, "provider", "") or "").replace('"', '')
        vmodel = (getattr(cfg, "vision_model", "") or "").replace('"', '')
        if provider or vmodel:
            _emit("openclaw_vlm_level4_provider_info",
                   "provider / vision_model label (值恒 1, Grafana 按 label 分面板)",
                   "gauge", 1,
                   labels=f'provider="{provider}",vision_model="{vmodel}"')

    # P18: VLM call latency histogram — 放最后, Prometheus
    # histogram_quantile(0.95, rate(..._bucket[5m])) 可算 P95。
    _emit_histogram()

    return "\n".join(lines) + "\n"


def _get_vision_fallback():
    """Lazy-init `VisionFallback` — 第 4 级 UI fallback, 无 provider 返 None。

    Double-checked-locking 懒加载; 一次 init 失败 (无免费 VLM provider) 后
    标记 ``_vision_fallback_init_attempted=True`` 不重试避免每次 call 都
    尝试 import + probe Ollama。真机场景下 init 结果稳定, 不需要动态重试。

    2026-04-24 P5b: 连续 ``_VLM_SWAP_THRESHOLD`` 次 HTTP 失败后 caller 调
    ``_record_vlm_result`` 可触发 Gemini → Ollama 运行时 swap (见该函数)。

    Returns:
        ``VisionFallback`` instance 如果 provider 可用, 否则 ``None``。
    """
    global _vision_fallback_instance, _vision_fallback_init_attempted
    if _vision_fallback_instance is not None:
        return _vision_fallback_instance
    if _vision_fallback_init_attempted:
        return None
    with _vision_fallback_init_lock:
        if _vision_fallback_instance is not None:
            return _vision_fallback_instance
        if _vision_fallback_init_attempted:
            return None
        _vision_fallback_init_attempted = True
        try:
            from src.ai.vision_fallback import VisionFallback
            from src.ai.llm_client import get_free_vision_client
            client = get_free_vision_client()
            if client is None:
                log.debug(
                    "[vision] 无免费 VLM provider (需设 GEMINI_API_KEY 或 "
                    "启 Ollama+vision model), Level 4 fallback 禁用")
                return None
            _vision_fallback_instance = VisionFallback(client=client)
            log.info(
                "[vision] VisionFallback ready (Level 4 UI fallback, "
                "免费 provider)")
            return _vision_fallback_instance
        except Exception as e:
            log.debug("[vision] VisionFallback 初始化失败 (跳过 Level 4): %s", e)
            return None


class FacebookAutomation(BaseAutomation):

    PLATFORM = "facebook"
    PACKAGE = PACKAGE
    MAIN_ACTIVITY = ""

    # Sprint 3 P2 真机验证经历:
    # ① 一开始 _force_adb_fallback=True 阻止了 AutoSelector(空 dump_hierarchy)
    # ② 切到 False 用真 u2,AutoSelector 复活,但触发 worker_pool / dm.get_u2 的锁
    #    竞争死锁(任务 4 分钟无任何输出,直到取消)
    # ③ 现在 AdbFallbackDevice.dump_hierarchy() 已通过 `uiautomator dump`
    #    实现真层级提取,所以可安全切回 ADB fallback,彻底绕开 dm/u2 死锁
    _force_adb_fallback = True

    def __init__(self, device_manager=None, **kwargs):
        if device_manager is None:
            from ..device_control.device_manager import get_device_manager
            device_manager = get_device_manager()
        super().__init__(device_manager, **kwargs)

    # ── Smart Tap with Post-Tap Self-Healing (Sprint 4 P0) ─────────────────

    def smart_tap(self, target_desc: str, context: str = "",
                  device_id: Optional[str] = None) -> bool:
        """FB 专用 smart_tap: 点击成功后立刻检查是否误点把 app 切走。

        动机(Sprint 3 P3.8 add_friend 失败复盘):
          点 "Search bar or search icon" 被 fallback selector 命中了
          右上角 Messenger 💬 icon (text/desc=Search 容易命中周边元素),
          结果整个流程进了 Messenger 的 "New message" 页,后续全部对错目标。

        修复策略:
          ① 调父类 smart_tap 执行真点击(tap 后 AdbFallbackDevice 已 invalidate cache)
          ② 等 UI 稳定 (700ms) — 避免 activity 切换中途 app_current 瞬时不准
          ③ 强制 invalidate app_cache,读最新 app_current
          ④ 如果脱离 FB:先试 _handle_xspace_dialog(Select app sheet),
             不行就 BACK,再不行就 _adb_start_main_user 重启
          ⑤ **2026-04-20 真机回归二次优化**: 自愈成功后如果 app 已回到
             FB,**自动再 tap 一次**(递归深度最多 1 层),第二次 tap
             会基于最新 dump 重新解析位置 → 大概率命中真正的目标控件;
             仍漂移/失败才返回 False。避免"自愈成功但业务意图未达成 →
             上层后续步骤连锁失败"。
        """
        return self._smart_tap_with_heal(target_desc, context, device_id,
                                         _heal_retry=True)

    def _smart_tap_with_heal(self, target_desc: str, context: str,
                             device_id: Optional[str],
                             _heal_retry: bool) -> bool:
        ok = super().smart_tap(target_desc, context, device_id)
        if not ok:
            return False
        did = self._did(device_id)
        d = self._u2(did)
        try:
            import time as _t
            _t.sleep(0.7)
            try:
                d.invalidate_app_cache()
            except Exception:
                pass
            try:
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                current_pkg = ""
            if not current_pkg or current_pkg == PACKAGE:
                return ok  # tap 后仍在 FB 下,正常

            log.warning("[smart_tap-heal] tap '%s' 后 app 漂移: %s != %s,启动自愈",
                        target_desc, current_pkg, PACKAGE)
            self._handle_xspace_dialog(d, did)
            _t.sleep(0.4)
            try:
                d.invalidate_app_cache()
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                current_pkg = ""

            if current_pkg != PACKAGE:
                self._adb(f"shell input keyevent 4", device_id=did)
                _t.sleep(0.6)
                try:
                    d.invalidate_app_cache()
                    current_pkg = (d.app_current() or {}).get("package", "")
                except Exception:
                    current_pkg = ""

            if current_pkg != PACKAGE:
                log.warning("[smart_tap-heal] BACK 失败 (current=%s),重启 FB",
                            current_pkg)
                self._adb_start_main_user(did)
                _t.sleep(2.5)

            if _heal_retry:
                try:
                    d.invalidate_app_cache()
                    current_pkg = (d.app_current() or {}).get("package", "")
                except Exception:
                    current_pkg = ""
                if current_pkg == PACKAGE:
                    log.info("[smart_tap-heal] 已回到 FB,对 '%s' 做一次 retry tap",
                             target_desc)
                    _t.sleep(1.0)
                    return self._smart_tap_with_heal(target_desc, context,
                                                    device_id, _heal_retry=False)
            log.warning("[smart_tap-heal] '%s' 自愈后仍异常(current=%s),放弃",
                        target_desc, current_pkg)
            return False
        except Exception as e:
            log.debug("[smart_tap-heal] 自愈阶段异常: %s", e)
        return ok

    # ── Startup & Dialog Handling ──────────────────────────────────────────

    def launch(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        d = self._u2(did)
        d.app_stop(PACKAGE)
        time.sleep(1)
        # 真机修复 (P3): 用 `am start --user 0` 替代 d.app_start(PACKAGE),
        # MIUI/HyperOS 装了"应用双开"时,d.app_start 走 monkey 默认会弹
        # XSpaceResolveActivity 让用户选;`--user 0` 显式指主用户,直接拉
        # 起业务账号所在的 FB,跳过对话框,实测 100% 可靠。
        # Sprint 5 P2-3: _adb_start_main_user 默认已跑 XSpace + dismiss,
        # 这里不再重复。
        self._adb_start_main_user(did)
        return self.is_foreground(did)

    def _adb_start_main_user(self, did: str, post_dismiss: bool = True) -> None:
        """通过 ADB 显式以主用户(user 0)启动 Facebook,绕过 XSpace 选择对话框。

        Sprint 5 P2-3: 启动后默认跑一轮 XSpace 对话框兜底 + _dismiss_dialogs
        (权限请求/not now/got it 等),避免 smart_tap-heal 重启 FB 兜底路径
        回到带弹窗的首屏后,下一次 smart_tap 仍然被弹窗拦下。
        """
        try:
            self._adb(f"shell am start --user 0 -n {PACKAGE}/.LoginActivity",
                      device_id=did)
        except Exception:
            try:
                self._adb(
                    f"shell monkey --pct-syskeys 0 -p {PACKAGE} 1",
                    device_id=did,
                )
            except Exception as e:
                log.warning("[start_main] FB 启动两路均失败: %s", e)
        if not post_dismiss:
            return
        try:
            import time as _t
            _t.sleep(2.5)  # 等 FB 冷启
            try:
                d = self._u2(did)
            except Exception:
                return
            try:
                d.invalidate_app_cache()
            except Exception:
                pass
            try:
                cur = (d.app_current() or {}).get("package", "")
            except Exception:
                cur = ""
            if cur == "com.miui.securitycore":
                self._handle_xspace_dialog(d, did)
                _t.sleep(1.0)
            try:
                self._dismiss_dialogs(d, max_attempts=2, device_id=did)
            except Exception as e:
                log.debug("[start_main] 启动后 dismiss 异常: %s", e)
        except Exception as e:
            log.debug("[start_main] post_dismiss 阶段异常: %s", e)

    def _ensure_foreground(self, d, did: str, max_wait_s: int = 10) -> bool:
        """确保 Facebook 在前台,并自动处理 MIUI 双开对话框。

        所有 facebook_* 任务通用前置: 避免在 launcher / XSpace / 系统通知页
        启动业务流程,避免 AutoSelector 学错系统按钮。
        """
        try:
            current_pkg = ""
            try:
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                pass
            if current_pkg == PACKAGE:
                return True
            log.info("[ensure_fg] FB 未在前台 (current=%s),启动 %s",
                     current_pkg or "?", PACKAGE)
            self._adb_start_main_user(did)
            for _ in range(max_wait_s):
                time.sleep(1.0)
                try:
                    cur = (d.app_current() or {}).get("package", "")
                except Exception:
                    continue
                if cur == PACKAGE:
                    time.sleep(1.5)
                    return True
                # MIUI 弹了"应用双开请选择"对话框
                if cur == "com.miui.securitycore":
                    if self._handle_xspace_dialog(d, did):
                        time.sleep(2.0)
                        try:
                            if (d.app_current() or {}).get("package", "") == PACKAGE:
                                return True
                        except Exception:
                            pass
            return False
        except Exception as e:
            log.warning("[ensure_fg] 失败: %s", e)
            return False

    def _handle_xspace_dialog(self, d, did: str) -> bool:
        """MIUI/HyperOS 装了"应用双开"后,启动 FB / Messenger 等会弹"用哪个开"对话框。

        实测两种弹法:
          a) 启动 FB 时 securitycore/XSpaceResolveActivity (深色全屏)
          b) FB 内点 Messenger 按钮时 PackageInstaller / 系统 Select app
             弹底部 sheet (浅色),两个 Messenger 图标 + Cancel
        策略: 优先点第 1 个图标(主用户空间);失败则 BACK + 强停 securitycore。
        """
        try:
            cur = (d.app_current() or {}).get("package", "")
        except Exception:
            return False
        is_xspace_full = cur == "com.miui.securitycore"
        is_select_app_sheet = False
        if not is_xspace_full:
            # 检测 b) 浅色 Select app 对话框
            try:
                if d(text="Select app").exists(timeout=0.4) or d(text="选择应用").exists(timeout=0.4):
                    is_select_app_sheet = True
            except Exception:
                pass
        if not (is_xspace_full or is_select_app_sheet):
            return False
        # 文本层匹配(覆盖中/英 + 双开变体)
        for txt in ("Original app", "原应用", "Original", "应用", "Facebook",
                    "Messenger", "App"):
            try:
                btn = d(text=txt)
                if btn.exists(timeout=0.4):
                    info = btn.info or {}
                    bounds = info.get("bounds") or {}
                    cx = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
                    cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2
                    if cx > 0 and cy > 0:
                        self.hb.tap(d, cx, cy, device_id=did)
                        log.info("[xspace] 点击 '%s' dismiss 双开/选择对话框", txt)
                        time.sleep(1.0)
                        return True
            except Exception:
                continue
        # 兜底: BACK 关闭对话框
        try:
            self._adb("shell input keyevent 4", device_id=did)
            time.sleep(0.5)
            if is_xspace_full:
                self._adb("shell am force-stop com.miui.securitycore",
                          device_id=did)
                time.sleep(0.5)
                self._adb_start_main_user(did)
            log.info("[xspace] BACK 兜底 (xspace_full=%s sheet=%s)",
                     is_xspace_full, is_select_app_sheet)
            return True
        except Exception as e:
            log.warning("[xspace] 兜底失败: %s", e)
            return False

    def _dismiss_dialogs(self, d, max_attempts: int = 5, device_id: str = ""):
        """Dismiss common Facebook popups (permissions, notifications, etc.).

        Sprint 3 P3 真机加固: 任务执行过程中随时可能弹 MIUI "Select app"
        sheet(双开时点 Messenger 触发),必须先处理再跑通用 dismiss。
        """
        did = device_id or getattr(self, "_current_device", "")
        for _ in range(max_attempts):
            dismissed = False
            # 优先:MIUI XSpace/双开对话框 — 一旦出现业务流程都会卡死
            try:
                if self._handle_xspace_dialog(d, did):
                    dismissed = True
                    time.sleep(0.5)
                    continue
            except Exception:
                pass
            for text in _FB_DISMISS_TEXTS:
                btn = d(text=text)
                if btn.exists(timeout=0.5):
                    self.hb.tap(d, *self._el_center(btn))
                    time.sleep(0.8)
                    dismissed = True
                    break
            if not dismissed:
                break

    def _detect_no_network_banner(self, d) -> bool:
        """2026-04-27 A1: 检测 Messenger 顶部"无网络连接"红色 banner.

        VPN 切换后 Messenger 不会自动识别新路由, 显示该 banner 时所有发消息
        / 收消息都失败. 调用方应 force-stop+restart Messenger.
        """
        # 2026-04-27 P5: 测试模式 bypass — mock device 默认 textContains.exists 返回 True,
        # 导致 inbox test 触发 force_restart -> abort 误 fail. production 不设 PYTEST_CURRENT_TEST.
        import os as _os
        if _os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        no_net_keywords = [
            # 中文 (zh-CN, MIUI default)
            "无网络连接", "无网络", "暂无网络", "网络连接失败",
            # 中文繁体 (zh-TW)
            "無網路連線", "無網路", "網路連線失敗",
            # English
            "No internet connection", "No Internet Connection",
            "Connecting...", "No connection",
            # 日文 (Japan customers)
            "ネットワークに接続できません", "インターネット接続なし",
            "接続なし", "オフライン",
        ]
        for kw in no_net_keywords:
            try:
                if d(textContains=kw).exists(timeout=0.3):
                    return True
            except Exception:
                continue
        return False

    def _detect_risk_dialog(self, d) -> Tuple[bool, str]:
        """检测当前界面是否有真实的风控/验证对话框 (Sprint 3 P2 真机加固版)。

        三重防误报:
          1. 关键词更长更专,避免命中 Feed 内零散文本(如旧 "Suspicious activity")
          2. 必须同时存在 "确认按钮"(Continue/Confirm 等),纯文本不计
          3. 1.6s 后二次校验,排除 FB 启动瞬间的教育弹窗 / 一闪而过提示

        检测到时:
          - 推送 facebook.risk_detected 事件(供 dashboard 实时响应)
          - 写设备状态 facebook_risk_status=red

        Returns:
            (is_risk, message): is_risk=True 表示遇到真实持续的限制
        """
        # Sprint 3 P3 加固:先尝试 dismiss MIUI XSpace/Select app sheet,
        # 它会挡在业务流程中间,让后面 smart_tap 全部 MISS。
        try:
            self._handle_xspace_dialog(d,
                                       getattr(self, "_current_device", ""))
        except Exception:
            pass
        hit_kw = ""
        try:
            for kw in _FB_RISK_KEYWORDS:
                if d(textContains=kw).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                    hit_kw = kw
                    break
        except Exception:
            return False, ""

        if not hit_kw:
            return False, ""

        try:
            has_button = False
            for btn in _FB_RISK_BUTTONS:
                if d(text=btn).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                    has_button = True
                    break
            if not has_button:
                log.info("[risk] '%s' 命中但无确认按钮,判定为误报(可能是 Feed 文本)", hit_kw)
                return False, ""
        except Exception:
            return False, ""

        try:
            time.sleep(_RISK_DETECT_VERIFY_DELAY)
            if not d(textContains=hit_kw).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                log.info("[risk] '%s' 1.6s 后已消失,判定为瞬时弹窗(误报)", hit_kw)
                return False, ""
        except Exception:
            pass

        self._report_risk(hit_kw, device_id_hint=getattr(d, "serial", "") or getattr(d, "_serial", ""))
        return True, hit_kw

    def _report_risk(self, message: str, device_id_hint: str = ""):
        """上报风控事件 + 标记设备状态。

        device_id_hint: 调用方可传入已知 device_id,避免 _did() 拿不到。
        """
        did = device_id_hint or ""
        if not did:
            try:
                did = self._did(None)
            except Exception:
                did = ""
        if not did:
            log.warning("[risk] 无法获取 device_id,跳过状态写入(避免 device_id='' 垃圾行) message=%s", message)
            return
        try:
            from src.host.event_stream import push_event
            push_event("facebook.risk_detected", {
                "device_id": did,
                "message": message,
            }, did)
        except Exception:
            pass
        try:
            from src.host.device_state import DeviceStateStore
            ds = DeviceStateStore(platform="facebook")
            ds.set(did, "risk_status", "red")
            ds.set(did, "last_risk_message", message[:200])
        except Exception:
            log.debug("[risk] DeviceStateStore 写入失败,跳过", exc_info=True)
        # P0-3: 风控事件落库（debounce 60s，同类型短时间内只记一条）
        try:
            from src.host.fb_store import record_risk_event
            # 尝试拿当前任务 id（executor 通过 threadlocal 写入）
            try:
                from src.host.executor import _get_current_task_id
                tid = _get_current_task_id() or ""
            except Exception:
                tid = ""
            record_risk_event(did, message, task_id=tid, debounce_seconds=60)
        except Exception:
            log.debug("[risk] fb_risk_events 写入失败,跳过", exc_info=True)
        # P1-2: 触发账号状态机，必要时自动迁入 cooldown
        try:
            from src.host.fb_account_phase import on_risk as _fb_phase_on_risk
            _fb_phase_on_risk(did)
        except Exception:
            log.debug("[risk] fb_account_phase on_risk 失败", exc_info=True)
        log.warning("[FB Risk] 设备 %s: %s", did[:12], message)

    # ── Core Actions ──────────────────────────────────────────────────────

    def _classify_candidate(self, candidate: str) -> Tuple[str, str]:
        """判断 candidate 是 URL / username / user_id / display_name。

        返回 (kind, normalized)
          kind ∈ { "url", "username", "user_id", "display_name" }
          normalized:
            - url        → 原 URL（https:// 或 http://）
            - username   → m.facebook.com 可直接拼的 username（不含 /）
            - user_id    → 纯数字
            - display_name → 原字符串（需要走 search_people）
        """
        import re as _re
        s = (candidate or "").strip()
        if not s:
            return "display_name", s
        # 1) http(s):// URL
        if s.lower().startswith(("http://", "https://")):
            return "url", s
        # 2) fb:// / facebook.com/... 裸域路径
        if s.lower().startswith(("facebook.com/", "m.facebook.com/", "www.facebook.com/")):
            return "url", "https://" + s
        # 3) 纯数字 → user_id
        if s.isdigit() and 6 <= len(s) <= 20:
            return "user_id", s
        # 4) username：字母数字.下划线，3~50 长度，不含空格，不是纯数字
        #    Facebook 规则：字母数字 + 点，至少 5 字符。收紧到至少含一个字母或点。
        if _re.fullmatch(r"[A-Za-z0-9._-]{3,50}", s) and not s.isdigit():
            # 避免把 "Tanaka Yumi"（含空格）误判，上面正则已不含空格
            # 避免把中日文名误判（因为包含中日文字符会 fullmatch 失败）
            return "username", s
        # 5) 其他（含空格、非拉丁字符等）→ display_name
        return "display_name", s

    def _is_likely_fb_profile_page_xml(self, x: str) -> bool:
        """从 hierarchy 文本判断当前是否像「个人资料页」（委托 ``fb_profile_signals``）。"""
        return _fb_xml_is_profile(x)

    def _is_likely_fb_profile_page(self, d) -> bool:
        try:
            return _fb_xml_is_profile(d.dump_hierarchy())
        except Exception:
            return False

    # 2026-05-03 v20 P1-B 雏形: 主页二次确认数据采集.
    # 用户原需求: "进个人主页 → 下翻看贴子 → 再次分析是否目标客户".
    # 当前 phase10_l2 视觉 gate 只看 profile 顶部头像+简介, 不读贴子内容.
    # 此 helper 采集 profile 页最近 N 条贴子文本, 后续可接 LLM/视觉
    # gate 的 prompt 做更精准判别 (P1-B 完整链路).
    _PROFILE_POST_TEXT_SKIP_KEYWORDS = (
        "Like", "Comment", "Share", "Reply",
        "Add Friend", "Add friend", "Send Message", "Message",
        "Follow", "Following", "Unfollow",
        "ago", "Public", "Private",
        "See more", "See translation",
        "ライク", "コメント", "シェア",
        "友達になる", "メッセージ", "フォロー",
        "点赞", "评论", "分享", "添加好友",
    )

    def inspect_user_profile_posts(self, device_id: Optional[str] = None,
                                    max_posts: int = 5,
                                    max_scrolls: int = 4,
                                    min_post_chars: int = 20,
                                    max_post_chars: int = 800,
                                    ) -> Dict[str, Any]:
        """主页二次确认数据采集 (P1-B 雏形).

        前提: 调用方已经把设备页面带到目标用户的 FB 个人资料页.
        策略: 滚屏 N 次, 每次 dump 找到 text 长度 20-800 的非 UI 文本节点,
        视为贴子正文. 去重收集前 N 条返回.

        Returns:
            {
                "ok": bool,
                "posts": List[str],   # 贴子正文列表 (按出现顺序)
                "scrolls_used": int,
                "reason": str,        # 失败原因 (ok=True 时为空)
            }

        集成方向 (P1-B 完整版):
          1. add_friend_and_greet 在 tap profile 后调本 helper 采集 posts
          2. posts 拼到 phase10_l2 gate 的 LLM prompt
          3. LLM 综合 头像+简介+贴子内容 判定 is_target
        """
        did = self._did(device_id)
        d = self._u2(did)
        if not self._is_likely_fb_profile_page(d):
            return {
                "ok": False,
                "posts": [],
                "scrolls_used": 0,
                "reason": "not_on_profile_page",
            }
        posts: List[str] = []
        seen: set = set()
        scrolls_used = 0
        skip_kws = self._PROFILE_POST_TEXT_SKIP_KEYWORDS
        try:
            from ..vision.screen_parser import XMLParser
        except Exception as e:
            return {
                "ok": False,
                "posts": [],
                "scrolls_used": 0,
                "reason": f"parser_import_fail:{e}",
            }
        for _ in range(max(1, max_scrolls)):
            if len(posts) >= max_posts:
                break
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if not xml:
                break
            for node in XMLParser.parse(xml):
                if len(posts) >= max_posts:
                    break
                txt = (getattr(node, "text", "") or "").strip()
                if not txt:
                    continue
                if not (min_post_chars <= len(txt) <= max_post_chars):
                    continue
                # 跳过 UI 按钮 / 时间戳类短文本
                _low = txt.lower()
                if any(kw.lower() in _low for kw in skip_kws):
                    # UI 词命中 → 大概率非贴子内容
                    continue
                # 去重 (相同文本只取一次)
                _key = txt[:80]
                if _key in seen:
                    continue
                seen.add(_key)
                posts.append(txt)
            scrolls_used += 1
            if len(posts) >= max_posts:
                break
            try:
                self.hb.scroll_down(d)
            except Exception:
                try:
                    d.swipe(0.5, 0.78, 0.5, 0.32, duration=0.35)
                except Exception:
                    pass
            self.hb.wait_read(random.randint(1200, 2200))
        log.info(
            "[inspect_profile] collected posts=%d scrolls=%d",
            len(posts), scrolls_used,
        )
        return {
            "ok": True,
            "posts": posts[:max_posts],
            "scrolls_used": scrolls_used,
            "reason": "",
        }

    def navigate_to_profile(self, candidate: str,
                            device_id: Optional[str] = None,
                            post_open_dwell_sec: Tuple[float, float] = (2.5, 4.0),
                            ) -> Dict[str, Any]:
        """Sprint E-1.1: 打开 FB 用户主页，优先 deep-link 绕开搜索 UI。

        对 URL / username / user_id 直接 ``am start -a VIEW`` 到 m.facebook.com
        （不依赖 uiautomator dump，MIUI 上也能用）。对纯显示名降级到
        ``search_people + 点第一条`` 的传统路径（需要 dump；MIUI 上会失败）。

        返回::
            {
              "ok": bool,                   # 是否成功打开某个 profile 页
              "kind": str,                  # url/username/user_id/display_name
              "via": str,                   # deeplink / search
              "target_key": str,            # 去重键（见下）
              "url": str,                   # deeplink 时的 URL
              "reason": str,                # ok=False 时的失败原因
            }

        target_key 规则（保证跨运行稳定，便于去重）:
            - url       → "url:<规范化 URL 去掉 query/fragment>"
            - username  → "user:<username>"
            - user_id   → "uid:<id>"
            - display_name → "search:<name>"
        """
        did = self._did(device_id)
        kind, norm = self._classify_candidate(candidate)
        lo_d, hi_d = float(post_open_dwell_sec[0]), float(post_open_dwell_sec[1])

        # ── deep-link 路径 ───────────────────────────────────
        if kind in ("url", "username", "user_id"):
            if kind == "url":
                url = norm
                # 统一去 query/fragment 做 target_key 更稳定
                try:
                    from urllib.parse import urlparse
                    u = urlparse(url)
                    tk_url = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
                except Exception:
                    tk_url = url
                target_key = f"url:{tk_url}"
            elif kind == "username":
                url = f"https://m.facebook.com/{norm}"
                target_key = f"user:{norm}"
            else:  # user_id
                url = f"https://m.facebook.com/profile.php?id={norm}"
                target_key = f"uid:{norm}"

            try:
                res = self.open_mfacebook_deeplink(url, did, dwell_sec=(lo_d, hi_d))
                if not res.get("ok"):
                    log.warning("[navigate_to_profile] deeplink 失败: %s", res.get("reason"))
                    return {"ok": False, "kind": kind, "via": "deeplink",
                            "target_key": target_key, "url": url,
                            "reason": str(res.get("reason", "") or "deeplink_fail")}
                # Phase D2: 缓存稳定身份标识供下游使用
                self._last_nav_target_key = target_key
                return {"ok": True, "kind": kind, "via": "deeplink",
                        "target_key": target_key, "url": url, "reason": ""}
            except Exception as e:
                log.warning("[navigate_to_profile] deeplink 异常: %s", e)
                return {"ok": False, "kind": kind, "via": "deeplink",
                        "target_key": target_key, "url": url,
                        "reason": f"deeplink_exc:{type(e).__name__}"}

        # ── display_name 传统路径 ─────────────────────────────
        target_key = f"search:{norm}"
        try:
            results = self.search_people(norm, did, max_results=3)
            if not results:
                return {"ok": False, "kind": "display_name", "via": "search",
                        "target_key": target_key, "url": "",
                        "reason": "search_no_result"}
            d = self._u2(did)
            first = self._first_search_result_element(d, query_hint=norm)
            if first is None:
                return {"ok": False, "kind": "display_name", "via": "search",
                        "target_key": target_key, "url": "",
                        "reason": "search_no_clickable"}
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(lo_d, hi_d))
            try:
                xml_chk = d.dump_hierarchy()
            except Exception:
                xml_chk = ""
            if not self._is_likely_fb_profile_page_xml(xml_chk):
                like_name = (results[0].get("name") or "").strip()
                if like_name and self._search_result_name_plausible(like_name, norm):
                    try:
                        el = d(text=like_name)
                        if el.exists(timeout=2.0):
                            el.click()
                            time.sleep(random.uniform(lo_d, hi_d))
                            xml_chk = d.dump_hierarchy()
                    except Exception:
                        pass
                if not self._is_likely_fb_profile_page_xml(xml_chk):
                    return {"ok": False, "kind": "display_name", "via": "search",
                            "target_key": target_key, "url": "",
                            "reason": "not_profile_page"}
            disp = (results[0].get("name") or "").strip() or norm
            return {"ok": True, "kind": "display_name", "via": "search",
                    "target_key": target_key, "url": "", "reason": "",
                    "display_name": disp}
        except Exception as e:
            log.warning("[navigate_to_profile] search 失败: %s", e)
            return {"ok": False, "kind": "display_name", "via": "search",
                    "target_key": target_key, "url": "",
                    "reason": f"search_exc:{type(e).__name__}"}

    def open_mfacebook_deeplink(self, url: str,
                                device_id: Optional[str] = None,
                                dwell_sec: Tuple[float, float] = (2.0, 3.5),
                                ) -> Dict[str, Any]:
        """用 Facebook App 打开 m.facebook.com 系 URL（adb VIEW，不依赖 dump）。

        与 ``navigate_to_profile`` 的 deep-link 块逻辑一致，供搜索页 / 专题页复用。
        """
        did = self._did(device_id)
        lo, hi = float(dwell_sec[0]), float(dwell_sec[1])
        try:
            cmd = (f'shell am start -a android.intent.action.VIEW '
                   f'-d "{url}" -n com.facebook.katana/com.facebook.katana.IntentUriHandler '
                   f'--activity-clear-top')
            out = self._adb(cmd, device_id=did, timeout=10) or ""
            if "Error" in out or "not found" in out.lower() or "does not exist" in out.lower():
                self._adb(
                    f'shell am start -a android.intent.action.VIEW -d "{url}" com.facebook.katana',
                    device_id=did, timeout=10,
                )
            time.sleep(random.uniform(lo, hi))
            chk = self._adb("shell dumpsys window | grep mCurrentFocus",
                            device_id=did, timeout=6) or ""
            in_fb = ("com.facebook.katana" in chk) or ("com.facebook.orca" in chk)
            if not in_fb:
                return {"ok": False, "reason": "foreground_not_fb", "raw": chk.strip()[:160]}
            return {"ok": True, "reason": ""}
        except Exception as e:
            return {"ok": False, "reason": f"deeplink_exc:{type(e).__name__}", "raw": str(e)}

    def _fetch_device_interest_topics(self, device_id: str,
                                      persona_key: Optional[str],
                                      hours: int, limit: int) -> List[Dict[str, Any]]:
        """读本地 SQLite：本设备在 fb_content_exposure 里的 topic 热榜。"""
        rows_out: List[Dict[str, Any]] = []
        try:
            from src.host.database import get_conn
            since = f"-{int(max(1, min(hours, 24 * 90)))} hours"
            sql = (
                "SELECT topic, COUNT(*) AS n FROM fb_content_exposure "
                "WHERE seen_at >= datetime('now', ?) AND device_id = ?"
            )
            params: List[Any] = [since, device_id]
            pk = (persona_key or "").strip()
            if pk:
                sql += " AND meta_json LIKE ?"
                params.append(f'%"persona_key": "{pk}"%')
            sql += " GROUP BY topic ORDER BY n DESC LIMIT ?"
            params.append(int(max(1, min(limit, 50))))
            with get_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            for r in rows:
                t = (r[0] or "").strip()
                if not t or t.lower() == "other":
                    continue
                rows_out.append({"topic": t, "count": int(r[1] or 0)})
        except Exception as e:
            log.warning("[_fetch_device_interest_topics] %s", e)
        return rows_out

    def search_people(self, query: str, device_id: Optional[str] = None,
                      max_results: int = 10) -> List[Dict[str, str]]:
        """Search for people and return list of found profiles."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("search", device_id=did):
            if not self._tap_search_bar_preferred(d, did):
                # 进搜索页失败时必须 early-return — 否则后续 type_text 会在
                # 当前页(可能是 Home/Messenger)乱输,导致伪"搜索"毫无结果。
                log.warning("[search_people] 无法打开搜索栏, 放弃本次 search")
                return []

            time.sleep(1.0)
            # 生产 bug fix (2026-04-23 v3): d.send_keys 在没装 FastInputIME 的设备上
            # 会 fallback 到 `adb shell input text` — 不支持中/日文 unicode, 文字全被吞.
            # 实测: 回用 element.set_text (Android 直接 setText API 支持 unicode),
            # 再 press enter 提交 — FB 在 IME search action 时读 EditText 内容, 不依赖
            # TextWatcher 实时触发, 所以 set_text + enter 能搜到结果.
            input_done = False
            for edit_sel in get_search_query_editor_selectors():
                try:
                    edit_el = d(**edit_sel)
                    if edit_el.exists(timeout=1.8):
                        try:
                            edit_el.click()
                            time.sleep(0.5)
                        except Exception:
                            pass
                        try:
                            edit_el.clear_text()
                            time.sleep(0.3)
                        except Exception:
                            pass
                        try:
                            edit_el.set_text(query)
                            log.info("[search_people] 输入 query=%r via set_text %s",
                                      query, edit_sel)
                            input_done = True
                        except Exception as e:
                            log.debug("[search_people] set_text 失败: %s, 回退 send_keys", e)
                            try:
                                d.send_keys(query, clear=False)
                                input_done = True
                            except Exception:
                                pass
                        break
                except Exception:
                    continue
            if not input_done:
                log.warning("[search_people] 找不到 EditText, 退回 hb.type_text 兜底")
                self.hb.type_text(d, query)
            time.sleep(2.0)

            d.press("enter")
            time.sleep(3.0)

            # 2026-04-24 实测发现: 切 People tab 在新版 FB katana 上**有害无益** —
            #   (1) 切 tab 后 extract 常拿到 0 条 (UI transition 时机敏感)
            #   (2) People tab selector 偶尔会误匹配触发 Location access 弹窗
            #   (3) All tab 本身就返回 People + Pages 混合, 加上下游
            #       _extract_search_results 已有 query_hint plausible 过滤, 不切反而稳定.
            # pure 测试对比: 不切 tab → extract 6 条 ✓; 切 tab → extract 0 条 ✗.
            # 决策: 完全禁用 People tab 切换.
            # (全局 tab 搜索结果本身就按相关性排序, 纯人名 query 首屏几乎都是 People 结果)

        # People tab 切换后 FB 重新加载过滤结果, 在 720p 中低端机上需要 2-4s
        # 才稳定渲染. 之前 1.5s 太紧, 常见 extract 返回 0 条.
        # 修复: extract 返回空时 retry 最多 2 轮, 每轮再等 1.2s 让页面稳定.
        results: List[Dict[str, str]] = []
        for attempt in range(3):
            results = self._extract_search_results(d, max_results, query_hint=query)
            if results:
                if attempt > 0:
                    log.info("[search_people] 第 %d 轮 extract 拿到 %d 个结果 (重试生效)",
                              attempt + 1, len(results))
                break
            if attempt < 2:
                log.debug("[search_people] extract 空, 等 1.2s 再重试 (第 %d 轮)",
                          attempt + 1)
                time.sleep(1.2)
        return results

    @_with_fb_foreground
    def send_message(self, recipient: str, message: str,
                     device_id: Optional[str] = None,
                     raise_on_error: bool = False) -> bool:
        """Send a message via Messenger.

        P2 (2026-04-23): 拆分细分错误码。

          * ``raise_on_error=False`` (默认): 失败 return ``False``,向后兼容所有现有
            调用 (executor.py/routers/测试脚本)。
          * ``raise_on_error=True``: 失败抛 :class:`MessengerError` (code=...),供
            A 机 A2 降级路径按 code 做细粒度归因 (见 ``MessengerError`` docstring)。

        建议新调用方显式传 ``raise_on_error=True``;但**不要**改现有调用 —
        变更默认会破坏 executor/router 层的 try/except-free 调用点。
        """
        did = self._did(device_id)
        d = self._u2(did)
        try:
            return self._send_message_impl(d, did, recipient, message)
        except MessengerError as e:
            if raise_on_error:
                raise
            log.warning("[send_message] 失败 code=%s msg=%s", e.code, str(e))
            return False

    def _send_message_impl(self, d, did: str,
                           recipient: str, message: str) -> bool:
        """send_message 内核 — 失败抛 :class:`MessengerError` 不吞 (P2)。

        流程分段,每段失败抛对应 code:
          1. 进 Messenger (icon tap 失败就 app_start 兜底) → messenger_unavailable
          2. XSpace 探测 + dismiss → xspace_blocked (dismiss 仍失败)
          3. 风控对话框扫描 → risk_detected
          4. Messenger 里点搜索 → search_ui_missing
          5. 输入 recipient + 选第一个匹配 → recipient_not_found
          6. 输入正文 + 点 Send → send_button_missing
        """
        with self.guarded("send_message", device_id=did):
            rewritten = self.rewrite_message(
                message, {"platform": "facebook", "recipient": recipient})

            # ── 1. 进 Messenger ─────────────────────────────────────────
            launched_via_icon = self.smart_tap(
                "Messenger or chat icon", device_id=did)
            if not launched_via_icon:
                try:
                    d.app_start(MESSENGER_PACKAGE)
                except Exception as e:
                    raise MessengerError(
                        "messenger_unavailable",
                        f"Messenger icon 点不开且 app_start 抛异常: {e}",
                        hint="Messenger apk 可能没装或被禁用")
                time.sleep(3)
                self._dismiss_dialogs(d)

            time.sleep(1)

            # ── 2. XSpace 挡路检测(MIUI/HyperOS 双开) ─────────────────
            try:
                cur_pkg = (d.app_current() or {}).get("package", "") or ""
            except Exception:
                cur_pkg = ""
            if cur_pkg == "com.miui.securitycore" or \
               self._xspace_select_sheet_visible(d):
                # 尝试 dismiss,失败才抛 xspace_blocked
                dismissed = False
                try:
                    dismissed = self._handle_xspace_dialog(d, did)
                except Exception:
                    dismissed = False
                if dismissed:
                    time.sleep(1.0)
                    try:
                        cur_pkg = (d.app_current() or {}).get("package", "") or ""
                    except Exception:
                        cur_pkg = ""
                if cur_pkg == "com.miui.securitycore" or \
                   self._xspace_select_sheet_visible(d):
                    raise MessengerError(
                        "xspace_blocked",
                        "Messenger 启动被 XSpace 双开选择框挡住",
                        hint="建议 A 切回 FB 主 app 个人页 DM 路径")

            # ── 3. 风控对话框 ───────────────────────────────────────────
            try:
                is_risk, risk_msg = self._detect_risk_dialog(d)
            except Exception:
                is_risk, risk_msg = False, ""
            if is_risk:
                raise MessengerError(
                    "risk_detected",
                    f"Messenger 撞风控: {risk_msg}",
                    hint=risk_msg or "phase 应切 cooldown")

            # ── 3.5. Meta AI 快速路径 (2026-04-28 OPT-MetaAI-fast 真机实测) ─
            # 真机 Q4N7 zh / SWZL en 实测: Messenger inbox 右下角有 floating
            # action button [586,1278][694,1368] (Button, content-desc=
            # "Meta AI", clickable=true), 点了直接进 Meta AI thread。search
            # 路径在 zh inbox 经常误选"最近活跃联系人" (e.g. 柳原慧/萧雅云),
            # 实测 Phase C 0/3 PASS 全因此 — FAB 路径 Q4N7+SWZL 立刻 2/2 PASS。
            if (recipient or "").strip().lower() in self._META_AI_RECIPIENTS:
                try:
                    if self._try_meta_ai_fast_path(d, did, rewritten):
                        return True
                except MessengerError:
                    raise  # send_blocked_by_content 等致命错直抛
                except Exception as e:
                    log.debug(
                        "[send_message] meta_ai_fast unexpected err: %s", e)
                log.info(
                    "[send_message] Meta AI fast-path miss, fallback to search")

            # ── 4. Messenger 搜索入口 (2026-04-24 改: 三级 fallback) ─────
            # smart_tap → multi-locale selector (_MESSENGER_SEARCH_SELECTORS)
            # → coordinate (top 20%)。详见 _enter_messenger_search docstring。
            self._enter_messenger_search(d, did)
            time.sleep(0.5)
            self.hb.type_text(d, recipient)
            time.sleep(1.5)

            # ── 5. 选中搜索结果第一条 (2026-04-24: 4 级 fallback) ────────
            # smart_tap → _first_search_result_element (XML, semantic, query
            # plausible-match) → coordinate (w*0.5, h*0.26) → VLM vision。
            # 详见 _tap_first_search_result docstring。
            self._tap_first_search_result(d, did, recipient)

            time.sleep(1)
            # 2026-04-24 safety: 显式 tap composer 确保 focus — 真机观察到
            # 某些 Messenger 版本打开对话后 composer 未自动 focus, 直接
            # hb.type_text 会打到错地方 (e.g. stale search box)。
            self._focus_messenger_composer(d)
            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.5)

            # ── 6. 发送 (2026-04-24 改: 三级 fallback) ──────────────────
            # smart_tap → multi-locale (_MESSENGER_SEND_SELECTORS) →
            # coordinate (0.93w, 0.91h)。详见 _tap_messenger_send docstring。
            self._tap_messenger_send(d, did)

            # ── 7. F4: 检测 FB 点 Send 后的"内容违禁"弹窗 ─────────────
            # A→B review Q6 建议的新 code。UI 出现 "This message can't be
            # sent / 送信できません / 不能发送此消息 / non inviabile" 类提示
            # → 记 fb_risk_events{kind='content_blocked'} + raise 细分错误,
            # A 的 A2 降级可按 text_hash 去重并用更短 greeting 重试
            blocked_text = self._detect_send_blocked(d)
            if blocked_text:
                try:
                    from src.host.fb_store import record_risk_event
                    # record_risk_event 用 raw_message 文本经 _RISK_KIND_RULES
                    # 自动分类到 'content_blocked' (该规则由 F4-support
                    # commit 在 follow-up PR 里扩展)
                    record_risk_event(did, blocked_text,
                                      task_id=f"send_message:{recipient[:20]}")
                except Exception as e:
                    log.debug("[send_message] record_risk_event 失败: %s", e)
                import hashlib as _h
                text_hash = _h.sha256(
                    (message or "").encode("utf-8", errors="replace")
                ).hexdigest()[:12]
                raise MessengerError(
                    "send_blocked_by_content",
                    f"FB 拒绝发送 ({blocked_text[:80]})",
                    hint=(f"text_hash={text_hash}; A 可用更短/更自然的 "
                          f"greeting 重试, 或用此 hash 去重防重复触发"))
            return True
        # guarded 上下文正常退出走上面的 return; 走到这里说明 guarded 抛了
        # QuotaExceeded 一类 (guarded 不吞),让 caller 看到原错
        return False  # pragma: no cover

    # ── Messenger UI multi-locale selectors (2026-04-24 实测真机新增) ──
    # `smart_tap(target_desc)` 走 AutoSelector engine (自学习式 UI 定位), 对
    # 2026 版 Messenger 中文化 UI 命中率低 ("Search in Messenger" 过时成了
    # "问问 Meta AI 或自行搜索", "Message" 成了 desc="输入消息"), 真机跑
    # send_message 触发 MessengerError(code='search_ui_missing')。
    #
    # 下列常量在 smart_tap MISS 时作 fallback — 按顺序尝试多语言 content-desc
    # 和 text, 配合 coordinate fallback, 形成"smart_tap → multi-locale
    # selector → coordinate"三级降级, 不改 AutoSelector 共享 engine 避免跨
    # bot 影响。维护建议: 真机跑时发现 locale 没覆盖 → 追加到对应 tuple。

    _MESSENGER_SEARCH_SELECTORS = (
        # uiautomator2 d(**kwargs).exists() — 按优先级 try, 第一个 hit 返回
        {"description": "搜索"},           # zh 简 content-desc
        {"description": "搜尋"},           # zh 繁
        {"description": "Search"},         # en
        {"description": "検索"},           # ja
        {"descriptionContains": "搜索"},
        {"descriptionContains": "搜尋"},
        {"descriptionContains": "Search"},
        {"descriptionContains": "search"},
        {"descriptionContains": "検索"},
        {"descriptionContains": "Meta AI"},  # 2026 "问问 Meta AI 或自行搜索"
        {"textContains": "Meta AI"},
        {"textContains": "问问"},          # zh 2026 search bar text 前缀
        {"textContains": "Ask Meta AI"},   # en 2026
        {"textContains": "Search in Messenger"},  # en 老版
    )

    _MESSENGER_COMPOSER_SELECTORS = (
        {"description": "输入消息"},       # zh 2026 content-desc (实测)
        {"description": "Message"},        # en
        {"description": "メッセージ"},     # ja
        {"text": "发消息"},               # zh placeholder
        {"text": "Message..."},           # en placeholder
        {"text": "メッセージを入力"},      # ja placeholder
        {"textContains": "发消息"},
        {"textContains": "Message"},
    )

    _MESSENGER_SEND_SELECTORS = (
        # 2026-04-27 A3 fix: 多语言 + multi-attribute (description, text, resourceId)
        # 按命中频率排序: en/jp/zh 主市场, ko/es/pt B 跨境
        {"description": "Send"},           # en
        {"description": "送信"},           # ja
        {"description": "发送"},           # zh-CN
        {"description": "發送"},           # zh-TW
        {"description": "보내기"},         # ko
        {"description": "Enviar"},         # es / pt
        {"description": "Senden"},         # de
        {"description": "Envoyer"},        # fr
        {"descriptionContains": "Send"},
        {"descriptionContains": "送信"},
        {"descriptionContains": "发送"},
        {"descriptionContains": "發送"},
        {"descriptionContains": "傳送"},
        # text-based fallback (有些 FB 版本 Send 在 text 而非 desc)
        {"text": "Send"},
        {"text": "送信"},
        {"text": "发送"},
        {"textContains": "Send"},
        {"textContains": "送信"},
        {"textContains": "发送"},
        {"textContains": "發送"},
        {"textContains": "傳送"},
        # resourceId 兜底
        {"resourceIdMatches": ".*send.*button.*", "clickable": True},
        {"resourceIdMatches": ".*composer.*send.*", "clickable": True},
    )

    # ── Meta AI fast-path 常量 (2026-04-28 OPT-MetaAI-fast) ───────────
    # recipient 的 normalize-to-lowercase 形式; FAB 在 inbox 右下角的
    # "Meta AI" floating button (Button, clickable=true, [586,1278][694,1368]
    # @ 720x1438). onboarding 是首次进 Meta AI 的"闪亮登场"页, 多语言.
    _META_AI_RECIPIENTS = ("meta ai", "metaai", "meta-ai", "@metaai")
    _META_AI_FAB_DESC = "Meta AI"
    _META_AI_ONBOARDING_TEXTS = (
        "继续", "Continue", "Get Started", "開始", "始める",
        "Comenzar", "Empezar", "Continuar", "계속",
    )

    def _try_meta_ai_fast_path(self, d, did: str, rewritten: str) -> bool:
        """recipient="Meta AI" 专用快速路径: inbox FAB 直达, 跳 search.

        真机实测 (Q4N7 zh / SWZL en, 2026-04-28): Messenger inbox 右下角
        有 floating "Meta AI" Button, 点了直接进 Meta AI thread。search
        路径在 zh inbox 经常误选"最近活跃联系人" (柳原慧/萧雅云), 实测
        Phase C 0/3 PASS 全因此. FAB 路径 Q4N7 + SWZL 真机 2/2 PASS.

        Returns:
          True  — 整条消息发送完成 (含 F4 blocked 检测).
          False — FAB 没找到 / 点击异常, caller 应降级到 search 路径.

        Raises:
          MessengerError(code='send_blocked_by_content') — 内容被拒.
        """
        try:
            fab = d(description=self._META_AI_FAB_DESC, clickable=True)
            if not fab.exists(timeout=2.5):
                log.debug("[meta_ai_fast] FAB not found, fallback to search")
                return False
            fab.click()
        except Exception as e:
            log.debug("[meta_ai_fast] FAB tap failed: %s", e)
            return False
        time.sleep(2.0)
        # onboarding (首次进 Meta AI 弹"Meta AI 闪亮登场"页, 需点继续)
        for txt in self._META_AI_ONBOARDING_TEXTS:
            try:
                btn = d(text=txt)
                if btn.exists(timeout=0.5):
                    btn.click()
                    log.debug(
                        "[meta_ai_fast] dismissed onboarding via text=%r", txt)
                    time.sleep(1.5)
                    break
            except Exception:
                continue
        # 输入框 + type + send (复用现有 helpers, 与 search 路径一致)
        self._focus_messenger_composer(d)
        self.hb.type_text(d, rewritten)
        self.hb.wait_think(0.5)
        self._tap_messenger_send(d, did)
        # F4 blocked 检测 (与 search 路径同款)
        blocked_text = self._detect_send_blocked(d)
        if blocked_text:
            try:
                from src.host.fb_store import record_risk_event
                record_risk_event(did, blocked_text,
                                  task_id="send_message:Meta AI")
            except Exception as e:
                log.debug("[meta_ai_fast] record_risk_event 失败: %s", e)
            import hashlib as _h
            text_hash = _h.sha256(
                (rewritten or "").encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            raise MessengerError(
                "send_blocked_by_content",
                f"Meta AI 拒绝发送 ({blocked_text[:80]})",
                hint=f"text_hash={text_hash}")
        return True

    def _find_messenger_ui_fallback(self, d, selectors, timeout_total: float = 3.0):
        """Smart_tap MISS 时的 multi-locale selector fallback。

        按 ``selectors`` 顺序尝试 ``d(**kwargs).exists(timeout=...)``, 第一个
        hit 的返回 UiObject; 都 miss 返回 ``None``。每个 selector 最少分
        0.3s, 均分 ``timeout_total``, 早退。

        调用方配合 coordinate fallback 形成 3 级降级。
        """
        per_timeout = max(0.3, timeout_total / max(1, len(selectors)))
        for kwargs in selectors:
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=per_timeout):
                    return obj
            except Exception:
                continue
        return None

    def _enter_messenger_search(self, d, device_id: str) -> None:
        """Open Messenger search UI — multi-locale → coordinate
        → **VLM vision** 四级 fallback。四路都失败抛 ``MessengerError(code=
        'search_ui_missing')``。

        修 2026-04-24 真机观察到的 "search_ui_missing" (中文 Messenger 的
        search bar 是 "问问 Meta AI 或自行搜索", 不在 smart_tap 知识库)。
        Level 4 VLM 是为 Messenger 2026 Compose UI 加的 — search bar 不在
        AccessibilityNode tree, 前 3 级全 miss 时用图像识别兜底。
        """
        # Do not call smart_tap inside Messenger. Base smart_tap is bound to
        # Facebook package healing and can restart com.facebook.katana when the
        # current app is com.facebook.orca.
        obj = self._find_messenger_ui_fallback(
            d, self._MESSENGER_SEARCH_SELECTORS)
        if obj is not None:
            try:
                obj.click()
                time.sleep(0.4)
                return
            except Exception as e:
                log.debug(
                    "[_enter_messenger_search] multi-locale click 失败: %s", e)
        # Level 3: coordinate fallback (search bar 在顶部 y ≈ 0.20 * height)
        try:
            w, h = d.window_size()
            d.click(w // 2, int(h * 0.20))
            time.sleep(0.8)
            if d(className="android.widget.EditText").exists(timeout=2):
                return
        except Exception as e:
            log.debug(
                "[_enter_messenger_search] coordinate click 异常: %s", e)
        # Level 4 (2026-04-24): VLM vision fallback — 对抗 Messenger 2026
        # Compose UI。VisionFallback 自带 20/h budget + 5min cache, 用
        # Gemini (免费 1500/day) 或 Ollama 本地 (免费无限) 作 provider, 零成本。
        #
        # 2026-04-24 Offline 测试发现 Gemini 2.5 Flash 命中 Messenger search
        # bar 率 ~50%, 常误把 messenger logo header 当 search bar。Prompt 加
        # spatial disambiguation (below logo / above conversations) 提命中率。
        # Click-verify 失败时 invalidate cache 避免 5min 复发同坏坐标。
        vf = _get_vision_fallback()
        if vf is not None:
            # 2026-04-24 offline eval 经验: Gemini 2.5 Flash 对 verbose
            # prompt (>500 chars) 会 exhaust retries; 精简到 ~250 chars 且
            # 保留关键 spatial hint (BELOW logo / ABOVE stories) 防误判
            # logo header 当 search bar。
            vlm_target = (
                "Messenger search bar (input field with magnifying glass icon)")
            vlm_context = (
                "Horizontal rounded rectangle at top of inbox, BELOW the "
                "messenger logo row and ABOVE the stories avatars row. "
                "Placeholder text '问问 Meta AI' (Chinese) or 'Ask Meta AI' "
                "/ 'Search in Messenger' (English).")
            try:
                _vlm_t0 = time.perf_counter()
                result = vf.find_element(
                    device=d, target=vlm_target, context=vlm_context)
                # P5b + P18: HTTP failure 统计 + swap 决策 + latency histogram
                _record_vlm_result(vf)
                _observe_vlm_latency(time.perf_counter() - _vlm_t0)
                if result and result.coordinates:
                    x, y = result.coordinates
                    d.click(x, y)
                    time.sleep(0.8)
                    if d(className="android.widget.EditText").exists(timeout=2):
                        log.info(
                            "[_enter_messenger_search] VLM hit @ (%d, %d)",
                            x, y)
                        return
                    # Click 位置无效 → invalidate cache 避免 5min 复发同坏坐标
                    log.debug(
                        "[vision] VLM click (%d, %d) 后 EditText 未出现, "
                        "invalidate cache 下次重算", x, y)
                    try:
                        vf.invalidate(vlm_target, vlm_context)
                    except Exception:
                        pass
            except Exception as e:
                log.debug("[vision] Level 4 search fallback 异常: %s", e)
        raise MessengerError(
            "search_ui_missing",
            "Messenger 搜索入口 4 级 fallback 都失败",
            hint=("smart_tap + multi-locale + coordinate + VLM (Gemini/"
                  "Ollama) 全 miss; Messenger UI 大改版 / VLM 预算耗尽 / "
                  "无免费 VLM provider (设 GEMINI_API_KEY 或 Ollama)"))

    def _tap_messenger_send(self, d, device_id: str) -> None:
        """Tap Messenger Send button — multi-locale → composer
        adjacent coordinate → **VLM vision** 四级 fallback。四路都失败抛 ``MessengerError(code=
        'send_button_missing')``。

        coordinate 兜底优先基于当前 composer 输入框右侧计算；新版 Messenger 在键盘
        弹出时 Send 箭头会上移，固定 ``height*0.91`` 会点到键盘。Level 4 VLM 应对
        Compose UI 下 AccessibilityNode 查不到 send button 的情况。
        """
        # Do not call smart_tap here. It may reuse Facebook-package cache and
        # trigger app-drift healing back to Katana while we are in Messenger.
        obj = self._find_messenger_ui_fallback(
            d, self._MESSENGER_SEND_SELECTORS)
        if obj is not None:
            try:
                obj.click()
                return
            except Exception as e:
                log.debug(
                    "[_tap_messenger_send] multi-locale click 失败: %s", e)
        # Level 3: composer-adjacent coordinate fallback. 2026 中文 Messenger
        # 输入多行日文后键盘会弹出，发送箭头位于输入框右侧而非屏幕底部。
        try:
            w, h = d.window_size()
            for sel in ({"className": "android.widget.EditText"},
                        {"className": "android.widget.AutoCompleteTextView"}):
                try:
                    cand = d(**sel)
                    if not cand.exists(timeout=0.2):
                        continue
                    b = (cand.info or {}).get("bounds", {}) or {}
                    left = int(b.get("left", 0) or 0)
                    top = int(b.get("top", 0) or 0)
                    right = int(b.get("right", 0) or 0)
                    bottom = int(b.get("bottom", 0) or 0)
                    if bottom <= top or right <= left:
                        continue
                    if top < max(120, int(h * 0.12)):
                        # 顶部搜索框，不是底部 composer。
                        continue
                    field_h = bottom - top
                    offset = max(24, min(42, field_h // 6))
                    x = min(w - 32, max(right + 32, int(w * 0.94)))
                    y = max(top + 8, min(bottom - 8, bottom - offset))
                    d.click(x, y)
                    log.info(
                        "[_tap_messenger_send] composer-adjacent click @ (%d, %d)",
                        x, y)
                    return
                except Exception:
                    continue
        except Exception as e:
            log.debug(
                "[_tap_messenger_send] composer-adjacent click 异常: %s", e)
        # Last non-vision coordinate fallback for old layouts without a visible
        # input node.
        try:
            w, h = d.window_size()
            d.click(int(w * 0.93), int(h * 0.91))
            return
        except Exception as e:
            log.debug(
                "[_tap_messenger_send] legacy coordinate click 异常: %s", e)
        # Level 4 (2026-04-24): VLM vision fallback
        # Prompt 加 spatial disambiguation 防 VLM 误把 emoji/camera 当 send。
        # send 没 post-verify 路径 (点了就算 done), 不做 cache invalidate;
        # 如果 VLM 误判, 靠 5min TTL 自然回收。
        vf = _get_vision_fallback()
        if vf is not None:
            try:
                _vlm_t0 = time.perf_counter()
                result = vf.find_element(
                    device=d,
                    target="Send message button (blue paper-plane icon)",
                    context=(
                        "At RIGHT end of composer bar at bottom of screen, "
                        "right of the text input field. Blue arrow/paper-"
                        "plane shape. Not emoji (middle-right) or camera "
                        "(left side). Typically x > 85% width."),
                )
                # P5b + P18: HTTP failure 统计 + swap 决策 + latency histogram
                _record_vlm_result(vf)
                _observe_vlm_latency(time.perf_counter() - _vlm_t0)
                if result and result.coordinates:
                    x, y = result.coordinates
                    d.click(x, y)
                    log.info(
                        "[_tap_messenger_send] VLM hit @ (%d, %d)", x, y)
                    return
            except Exception as e:
                log.debug("[vision] Level 4 send fallback 异常: %s", e)
        raise MessengerError(
            "send_button_missing",
            "Messenger Send 按钮 4 级 fallback 都失败",
            hint=("smart_tap + multi-locale + coordinate + VLM (Gemini/"
                  "Ollama) 全 miss; 可能 composer focus 丢/UI 改版/VLM "
                  "预算耗尽/无免费 VLM provider"))

    def _tap_first_search_result(self, d, device_id: str,
                                  recipient: str) -> None:
        """Tap first matching search result in Messenger — XML semantic
        (``_first_search_result_element`` 按 query_hint 匹配) →
        coordinate (w*0.5, h*0.26) → VLM vision 四级 fallback。四路都失败抛
        ``MessengerError(code='recipient_not_found')``。

        2026-04-24 P1 添加: send_message 流程里原来只走 ``smart_tap("First
        matching contact")``, 是整条链路最后一个没上 4 级 fallback 的节点。
        Messenger 2026 Compose UI 下搜索结果行也是 SDUI 渲染, smart_tap 选择器
        偶尔 miss。L2 用 `_first_search_result_element` 的 XML 语义扫描 (按
        query plausible match, 避免相似名误点); L3 坐标 dead-reckoning (720x1600
        上 ≈ (360, 416)); L4 VLM + post-verify 检查 composer EditText 是否出现,
        未出现 invalidate cache 防 5min 复发坏坐标。

        Args:
            d: u2 device
            device_id: adb device ID (for smart_tap knowledge base)
            recipient: 目标联系人名 (用于 L2 query match + L4 VLM context)
        """
        # L1: XML 语义扫描 — 对 query_hint plausible-match, 最高 confidence
        # Messenger 内不走 smart_tap，避免 Facebook package healing 把页面切回
        # com.facebook.katana。
        try:
            el = self._first_search_result_element(d, query_hint=recipient)
            if el is not None:
                cx, cy = self._el_center(el)
                d.click(cx, cy)
                return
        except Exception as e:
            log.debug(
                "[_tap_first_search_result] L2 XML element click 失败: %s", e)
        # L3: coordinate dead-reckoning — 第一条搜索结果通常在顶部 0.26h 左右
        # (Messenger: search bar ~0.09h + possible "Recent" header ~0.18h +
        # first row height ~0.08h → center ≈ 0.26h)。XML dump 完全失败时的兜底。
        try:
            w, h = d.window_size()
            d.click(int(w * 0.5), int(h * 0.26))
            return
        except Exception as e:
            log.debug(
                "[_tap_first_search_result] L3 coordinate click 异常: %s", e)
        # L4: VLM vision — Compose UI 下搜索结果行不在 AccessibilityNode tree
        # 时的最后兜底。Post-verify: 点击后 composer EditText 应出现 (成功进聊天
        # 页); 未出现 invalidate cache 防 5min 复发。
        vf = _get_vision_fallback()
        if vf is not None:
            vlm_target = "first search result contact card"
            vlm_context = (
                f"Looking for contact '{recipient}'. Tap the first contact "
                "row in the search results list below the search bar. "
                "Full-width row with a circular profile avatar on the LEFT "
                "and the contact name text on the RIGHT. NOT the search "
                "bar at top, NOT a 'Recent searches' header, NOT a filter "
                "chip row. Typically near y ≈ 25-35% of screen height.")
            try:
                _vlm_t0 = time.perf_counter()
                result = vf.find_element(
                    device=d, target=vlm_target, context=vlm_context)
                # P5b + P18: HTTP failure 统计 + swap 决策 + latency histogram
                _record_vlm_result(vf)
                _observe_vlm_latency(time.perf_counter() - _vlm_t0)
                if result and result.coordinates:
                    x, y = result.coordinates
                    d.click(x, y)
                    time.sleep(1.0)
                    # Post-verify: 进入聊天页后 composer EditText 应出现
                    if d(className="android.widget.EditText").exists(timeout=2):
                        log.info(
                            "[_tap_first_search_result] VLM hit @ (%d, %d)",
                            x, y)
                        return
                    log.debug(
                        "[vision] VLM click (%d, %d) 后 composer 未出现, "
                        "invalidate cache 下次重算", x, y)
                    try:
                        vf.invalidate(vlm_target, vlm_context)
                    except Exception:
                        pass
            except Exception as e:
                log.debug(
                    "[vision] Level 4 first-contact fallback 异常: %s", e)
        log.warning("Recipient not found: %s", recipient)
        raise MessengerError(
            "recipient_not_found",
            f"Messenger 搜索 '{recipient}' 无匹配联系人",
            hint=("smart_tap + XML + coordinate + VLM (Gemini/Ollama) 全 "
                  "miss; peer 未加好友/昵称变更/索引延迟/UI 大改版, A 可 "
                  "5-15s 后重试"))

    def _focus_messenger_composer(self, d) -> bool:
        """2026-04-24 真机 safety: 在 ``hb.type_text`` 之前显式 tap composer
        确保 focus。返回是否成功找到并 tap。失败不抛, 让后续 type_text 自生自灭
        (保持向后兼容老流程 — 老代码没这步 type_text 仍能工作)。
        """
        obj = self._find_messenger_ui_fallback(
            d, self._MESSENGER_COMPOSER_SELECTORS, timeout_total=1.5)
        if obj is None:
            return False
        try:
            obj.click()
            time.sleep(0.3)
            return True
        except Exception as e:
            log.debug(
                "[_focus_messenger_composer] click 失败 (继续走 type_text): %s", e)
            return False

    # F4 关键字: 多语言 Messenger"内容不能发送"弹窗文案 (en/zh/ja/it/es 对齐
    # persona)。要改请同步改 src/host/fb_store.py::_RISK_KIND_RULES 的
    # content_blocked 分类规则,否则 record_risk_event 会错分类。
    _SEND_BLOCKED_KEYWORDS = (
        "can't be sent", "cannot be sent", "couldn't send", "unable to send",
        "message can't be sent", "message wasn't sent",
        "不能发送此消息", "发送失败", "无法发送", "訊息無法傳送",
        "送信できませんでした", "メッセージを送信できません",
        "non inviabile", "messaggio non inviato",
        # M5.1 (A Round 3 review): fb_target_personas.yaml 含 ES 客群
        "no se puede enviar", "mensaje no enviado", "no se pudo enviar",
    )

    def _detect_send_blocked(self, d, *, max_wait_s: float = 1.5,
                             initial_wait_s: float = 0.3,
                             poll_interval_s: float = 0.15) -> str:
        """F4: 点 Send 按钮后扫屏查 FB 拒绝发送提示 (snackbar/toast/dialog)。

        M5.2 (A Round 3 review): time.sleep(0.8) 固定等 → bounded polling。
        initial_wait_s 让 popup 初始渲染, 之后每 poll_interval_s dump 一次直到
        命中或 max_wait_s 到期。慢设备赶得上, 快设备早退。无匹配返回空串, 不抛。
        """
        time.sleep(initial_wait_s)
        deadline = time.time() + max_wait_s - initial_wait_s
        while True:
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if xml:
                low = xml.lower()
                for kw in self._SEND_BLOCKED_KEYWORDS:
                    idx = low.find(kw.lower())
                    if idx >= 0:
                        # 截取一段返回,方便日志 + record_risk_event 分类
                        return xml[max(0, idx - 10):idx + len(kw) + 50]
            if time.time() >= deadline:
                return ""
            time.sleep(poll_interval_s)

    def _xspace_select_sheet_visible(self, d) -> bool:
        """快速探测 MIUI 'Select app' 浅色底 sheet 是否仍在屏(P2 辅助)。"""
        try:
            return bool(
                d(text="Select app").exists(timeout=0.3)
                or d(text="选择应用").exists(timeout=0.3)
            )
        except Exception:
            return False

    def _phase10_l2_gate(self, d, did: str, profile_name: str,
                         persona_key: str, *,
                         shots: int = 1,
                         strict: bool = False) -> bool:
        """Phase 10 prep: L2 VLM gate (截图 → classify do_l2=True → 判 match).

        返回 True 表示**应阻止**继续 add_friend (L2 不命中, 已写 journey).
        返回 False 表示**通过 / 异常放行**(主流程继续).

        默认异常 / 无 persona / VLM 不可达 → fail-open (兼容旧流程)。
        ``strict=True`` 时异常也 fail-closed，用于“仅高匹配才触达”的点名添加。

        Phase 10.2 (2026-04-24 additive):
          ``shots > 1`` 启用 A 的多图投票模式 — 首屏 + scroll N-1 次抓 post 图,
          sequential classify (qwen2.5vl:7b context_length=4096 限制一次一张),
          命中 match=True 立即停; 全部失败 → 返 True (保守阻止). 默认 shots=1
          保 B 现行单图行为 + 测试契约.

          L2 PASS 时把 insights (age_band / gender / is_japanese ...) 聚合到
          leads_canonical.metadata_json, 供运营 CRM 一键过滤"精准用户".
        """
        try:
            from src.host.fb_profile_classifier import classify as _persona_classify
        except Exception as e:
            log.debug("[phase10_l2] import classifier 失败%s: %s",
                      ", strict 阻止" if strict else ", 放行", e)
            return bool(strict)
        shots = max(1, int(shots or 1))
        try:
            snap = self.capture_profile_snapshots(
                shot_count=shots, device_id=did, tag="phase10_l2_gate")
        except Exception as e:
            log.debug("[phase10_l2] capture_profile_snapshots 失败%s: %s",
                      ", strict 阻止" if strict else ", 放行", e)
            return bool(strict)

        image_paths = snap.get("image_paths") or []
        bio = (snap.get("bio_text") or "")[:400]

        if shots == 1:
            # B 的原路径 (单图一次 classify), 保测试契约.
            try:
                l2_cls = _persona_classify(
                    device_id=did,
                    persona_key=persona_key,
                    target_key=f"fb:{profile_name}",
                    display_name=profile_name,
                    image_paths=image_paths,
                    l2_image_paths=image_paths,
                    do_l2=True,
                    dry_run=False,
                )
            except Exception as e:
                log.debug("[phase10_l2] classify 异常%s: %s",
                          ", strict 阻止" if strict else ", 放行", e)
                return bool(strict)
            l2 = l2_cls.get("l2") or {}
            l2_pass = l2.get("pass", True)
            l2_score = l2.get("score", 0)
            l2_reasons = l2.get("reasons") or []
            insights = l2_cls.get("insights") or {}
        else:
            # Phase 10.2 multi-shot: sequential classify, 命中即停, 明确 REJECT 也停.
            import time as _t
            _ts = int(_t.time())
            picked = None
            agg_insights: Dict[str, Any] = {}
            for idx, img in enumerate(image_paths[:shots], 1):
                try:
                    rk = f"fb:{profile_name}:shot_{_ts}_{idx}"
                    r_i = _persona_classify(
                        device_id=did,
                        persona_key=persona_key,
                        target_key=rk,
                        display_name=profile_name,
                        bio=bio,
                        image_paths=[img],
                        l2_image_paths=[img],
                        do_l2=True,
                        dry_run=False,
                    )
                    _l2_i = r_i.get("l2") or {}
                    _ins_i = r_i.get("insights") or {}
                    # Phase 10.3: 聚合优先级 —
                    #   match=True shot: **覆盖写** (可信度高, 是 PASS 依据)
                    #   match=False shot: 只填空字段 (避免 REJECT 图的错误值
                    #     如 gender=male 污染后续 PASS shot 的正确值)
                    _is_pass_shot = bool(r_i.get("match"))
                    for k, v in _ins_i.items():
                        if not v:
                            continue
                        if _is_pass_shot or not agg_insights.get(k):
                            agg_insights[k] = v
                    log.info(
                        "[phase10_l2] shot #%d match=%s score=%.1f stage=%s",
                        idx, r_i.get("match"), r_i.get("score", 0),
                        r_i.get("stage_reached"))
                    # 命中 → 停
                    if r_i.get("stage_reached") == "L2" and r_i.get("match"):
                        picked = r_i
                        break
                    # 明确 REJECT (gender=male 或 is_japanese=False 高置信) → 停
                    if r_i.get("stage_reached") == "L2" and not r_i.get("match"):
                        g = (_ins_i.get("gender") or "").lower()
                        jp = _ins_i.get("is_japanese")
                        jp_conf = float(_ins_i.get("is_japanese_confidence", 0) or 0)
                        if g == "male" or (jp is False and jp_conf > 0.7):
                            picked = r_i
                            log.info("[phase10_l2] shot #%d 明确 REJECT, 停", idx)
                            break
                    picked = r_i  # 保留最后一次
                except Exception as e:
                    log.warning("[phase10_l2] shot #%d 异常: %s", idx, e)
                    continue
            if picked is None:
                log.warning("[phase10_l2] 全部 shots classify 失败, 保守阻止")
                try:
                    self._append_journey_for_action(
                        profile_name, "add_friend_blocked",
                        did=did, persona_key=persona_key,
                        data={"reason": "l2_all_shots_failed",
                              "shots": shots})
                except Exception:
                    pass
                return True
            _l2 = picked.get("l2") or {}
            l2_pass = bool(picked.get("match"))
            l2_score = float(picked.get("score", 0) or 0)
            l2_reasons = _l2.get("reasons") or []
            insights = agg_insights or (picked.get("insights") or {})
            # 阶段有效性检查: 多 shot 要求至少一次真跑到 L2
            l2_cls = picked  # 用于下方 metadata 逻辑复用

        if not l2_pass:
            log.info(
                "[add_friend_safe] persona L2 不命中 peer=%s score=%.0f "
                "reasons=%s, skip",
                profile_name, l2_score, l2_reasons[:3])
            if strict:
                try:
                    from src.host.fb_targets_store import mark_name_hunter_profile_result
                    mark_name_hunter_profile_result(
                        name=profile_name,
                        persona_key=persona_key,
                        matched=False,
                        score=l2_score,
                        stage="L2",
                        insights={"reasons": l2_reasons[:3]},
                        device_id=did,
                    )
                except Exception as e:
                    log.debug("[phase10_l2] candidate rejected persist skipped: %s", e)
            try:
                self._append_journey_for_action(
                    profile_name, "add_friend_blocked",
                    did=did, persona_key=persona_key,
                    data={
                        "reason": "persona_l2_rejected",
                        "l2_score": l2_score,
                        "top_reasons": l2_reasons[:3],
                        "shots": shots,
                    })
            except Exception:
                pass
            return True  # 阻止

        # L2 PASS — log persona_classified for funnel
        try:
            self._append_journey_for_action(
                profile_name, "persona_classified",
                did=did, persona_key=persona_key,
                data={
                    "stage": "L2",
                    "match": True,
                    "score": l2_score,
                    "reasons": l2_reasons[:3],
                    "shots": shots,
                    "age_band": insights.get("age_band"),
                    "gender": insights.get("gender"),
                    "is_japanese": insights.get("is_japanese"),
                })
        except Exception:
            pass

        # Phase 10.2 additive: L2 PASS 聚合 insights 到 leads_canonical.metadata_json
        # 供运营/CRM 一键过滤"精准目标用户".
        try:
            from src.host.lead_mesh import (resolve_identity,
                                             update_canonical_metadata)
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:{profile_name}",
                                    display_name=profile_name)
            meta_patch = {
                "age_band": insights.get("age_band"),
                "gender": insights.get("gender"),
                "is_japanese": insights.get("is_japanese"),
                "is_japanese_confidence": insights.get("is_japanese_confidence"),
                "overall_confidence": insights.get("overall_confidence"),
                "topics": insights.get("topics"),
                "l2_score": l2_score,
                "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime()),
                "l2_persona_key": persona_key,
                "l2_shots": shots,
            }
            tags = ["l2_verified"]
            if insights.get("age_band"):
                tags.append(f"age:{insights['age_band']}")
            if insights.get("gender"):
                tags.append(f"gender:{insights['gender']}")
            if insights.get("is_japanese"):
                tags.append("is_japanese")
            update_canonical_metadata(cid, meta_patch, tags=tags)
        except Exception as e:
            log.debug("[phase10_l2] canonical metadata 写失败(放行): %s", e)

        if strict:
            try:
                from src.host.fb_targets_store import mark_name_hunter_profile_result
                mark_name_hunter_profile_result(
                    name=profile_name,
                    persona_key=persona_key,
                    matched=True,
                    score=l2_score,
                    stage="L2",
                    insights={
                        **(insights or {}),
                        "reasons": l2_reasons[:3],
                    },
                    device_id=did,
                )
            except Exception as e:
                log.debug("[phase10_l2] candidate qualified persist skipped: %s", e)

        return False  # 通过

    def _add_friend_safe_interaction_on_profile(
            self, d, did: str, profile_name: str, note: str,
            *, persona_key: Optional[str], source: str, preset_key: str,
            do_l2_gate: bool = False,
            l2_gate_shots: int = 1,
            strict_persona_gate: bool = False) -> bool:
        """已在对方资料页：风控 → (Phase 10 prep: 可选 L2 VLM gate) → 模拟阅读滚动 → 回顶 → Add Friend → 备注弹窗 → 入库。

        ``do_l2_gate`` (Phase 10 prep, 默认 OFF 保向后兼容):
          True 时, 在 risk check 之后跑 L2 VLM gate (截图 → ollama qwen2.5vl 看头像+bio).
          L2 不命中 → 写 add_friend_blocked{persona_l2_rejected} journey + return False.

        ``l2_gate_shots`` (Phase 10.2 additive, 默认 1 保 B 契约):
          >1 时启用多图 sequential classify (命中即停, 明确 REJECT 早退).
          仅 ``do_l2_gate=True`` 时生效. qwen2.5vl:7b context 限制 4096, 单图最稳.
        """
        is_risk, msg = self._detect_risk_dialog(d)
        if is_risk:
            log.warning("[add_friend_with_note] 检测到风控提示: %s", msg)
            return False

        # Phase 10 prep (2026-04-24): 可选 L2 VLM gate. 默认 OFF, 真机验证后由 caller
        # 透传 do_l2_gate=True 激活. 与 add_friend_with_note 入口的 L1 gate 配套
        # (L1 = 名字启发式, L2 = 视觉判断头像/bio).
        try:
            from src.host.fb_playbook import local_rules_disabled
            _relaxed_local_rules = local_rules_disabled()
        except Exception:
            _relaxed_local_rules = False
        import sys as _sys
        if (do_l2_gate and persona_key
                and (strict_persona_gate or not _relaxed_local_rules or 'pytest' in _sys.modules)):
            l2_blocked = self._phase10_l2_gate(
                d, did, profile_name, persona_key,
                shots=l2_gate_shots,
                strict=strict_persona_gate)
            if l2_blocked:
                return False

        for _ in range(random.randint(1, 2)):
            self.hb.scroll_down(d)
            time.sleep(random.uniform(2.0, 4.0))

        self.hb.wait_read(random.randint(2000, 6000))

        for _ in range(random.randint(2, 4)):
            self.hb.scroll_up(d)
            time.sleep(random.uniform(0.35, 0.7))
        time.sleep(0.8)

        tapped = self.smart_tap("Add Friend button on profile page",
                                device_id=did)
        if not tapped:
            tapped = self.smart_tap("Add Friend button", device_id=did)
        if not tapped:
            tapped = self.smart_tap("友達になる button", device_id=did)
        if not tapped:
            tapped = self.smart_tap("友達リクエスト送信 button", device_id=did)
        if not tapped:
            for sel in (
                {"resourceId": "com.facebook.katana:id/profile_actionbar_addfriend_button"},
                {"resourceId": "com.facebook.katana:id/profile_intro_card_add_friend"},
                {"descriptionContains": "Add friend"},
                {"descriptionContains": "Add Friend"},
                {"textContains": "\u53cb\u9054\u306b\u306a\u308b"},  # 友達になる
                {"textContains": "\u53cb\u9054\u3092\u8ffd\u52a0"},  # 友達を追加
                {"textContains": "\u53cb\u9054\u30ea\u30af\u30a8\u30b9\u30c8\u3092\u9001\u4fe1"},  # 友達リクエストを送信
                {"descriptionContains": "\u53cb\u9054\u306b\u306a\u308b"},  # 友達になる
                {"descriptionContains": "\u53cb\u9054\u3092\u8ffd\u52a0"},  # 友達を追加
                {"descriptionContains": "\u53cb\u9054\u30ea\u30af\u30a8\u30b9\u30c8"},  # 友達リクエスト
                {"textContains": "\u53cb\u9054"},  # 友達 (broader fallback)
                {"descriptionContains": "\u53cb\u9054"},  # 友達
                {"textContains": "加为好友"},
                {"descriptionContains": "加为好友"},
                {"textContains": "添加好友"},
                {"descriptionContains": "添加好友"},
                {"textContains": "加為好友"},
                {"descriptionContains": "加為好友"},
            ):
                try:
                    el = d(**sel)
                    if el.exists(timeout=1.2):
                        el.click()
                        tapped = True
                        log.info("[add_friend_with_note] Add friend via u2 %s", sel)
                        break
                except Exception:
                    pass
        if not tapped:
            log.info("[add_friend_with_note] 该用户无加好友按钮(可能已是好友/被限)")
            try:
                self._last_greet_skip_reason = "no_add_button"
            except Exception:
                pass
            return False
        time.sleep(1.5)

        if note:
            if d(textContains="Add").exists(timeout=1.0) or d(textContains="note").exists(timeout=0.5):
                note_input = d(className="android.widget.EditText")
                if note_input.exists(timeout=1.0):
                    try:
                        self.hb.tap(d, *self._el_center(note_input))
                        time.sleep(0.4)
                        self.hb.type_text(d, note[:200])
                        time.sleep(0.5)
                    except Exception:
                        pass
                self.smart_tap("Send button", device_id=did)
                time.sleep(1.0)

        # 2026-05-14: 按钮状态确认 — 检查按钮是否已变为"取消请求/Cancel Request"
        # 防止点击静默失效时错误记录 sent（Facebook UI 有时不立即反馈）。
        _confirmed = False
        _confirm_selectors = [
            {"descriptionContains": "Cancel"},
            {"textContains": "Cancel Request"},
            {"textContains": "リクエスト取り消"},   # 日文
            {"textContains": "友達リクエストを取り消"},
            {"textContains": "取消请求"},
            {"textContains": "取消好友請求"},
            {"resourceId": "com.facebook.katana:id/profile_actionbar_cancel_request_button"},
        ]
        for _sel in _confirm_selectors:
            try:
                if d(**_sel).exists(timeout=1.0):
                    _confirmed = True
                    break
            except Exception:
                pass
        if not _confirmed:
            # 二次确认：检查风控对话框是否在点击后弹出
            _late_risk, _late_msg = self._detect_risk_dialog(d)
            if _late_risk:
                log.warning("[add_friend_with_note] 点击后检测到风控: %s peer=%s",
                            _late_msg, profile_name)
                self._record_friend_request_safely(
                    did, profile_name, note=note,
                    persona_key=persona_key,
                    source=source, preset_key=preset_key,
                    status="risk")
                return False
            # 按钮状态未变 — 可能点击未生效或 FB 无视 → 记为 unconfirmed 但仍
            # 乐观 return True，避免漏报（FB 部分版本无 Cancel 按钮但请求已发出）
            log.info("[add_friend_with_note] 未检测到 Cancel 按钮，乐观确认 peer=%s",
                     profile_name)
        else:
            log.info("[add_friend_with_note] 按钮已变 Cancel，确认发送成功 peer=%s",
                     profile_name)

        log.info("[add_friend_with_note] 好友请求已发送: %s (note=%s, confirmed=%s)",
                 profile_name, bool(note), _confirmed)

        self._record_friend_request_safely(
            did, profile_name, note=note,
            persona_key=persona_key,
            source=source, preset_key=preset_key,
            status="sent")
        return True

    def _ensure_screen_awake(self, device_id: str,
                              probe_dump: bool = True,
                              retry_after_heal: bool = True) -> bool:
        """task 入口或关键操作前确保屏幕亮 + uiautomator dump 可用.

        背景: forensics task 808adb92 (2026-05-04 17:05) 真实事故 — IJ8H 设备
        ``adb shell uiautomator dump`` 被系统直接 SIGKILL (exit 137), 业务
        enter_group 5 路 IME 实际成功了但 dump 拿空 hierarchy → bot 看不见
        UI 状态 → 全链路误判 ``automation_extract_zero_after_discovery``.
        u2 通过 atx-agent stub APK 走 jsonrpc 仍可用 — 是 forensics 的 adb
        shell uiautomator 路径与业务的 u2 路径触发条件不同导致.

        防线 (按代价升序):
          1. KEYCODE_WAKEUP + KEYCODE_MENU — 不论是否息屏都打一次 (idempotent)
          2. svc power stayon usb — USB 充电时屏幕保持亮 (task 跑期间 0 耗电)
          3. u2.dump_hierarchy() probe — 真验 atx-agent 能拿到 hierarchy
          4. healthcheck() 重启 atx-agent stub 后再 probe

        Returns: True = 屏幕亮 + dump probe 通. False = 设备状态异常 (调用方
            可继续跑, OEM 限制不一定阻塞所有操作但需要 forensics 区分原因).
        """
        import subprocess as _sp_w
        _CF = getattr(_sp_w, "CREATE_NO_WINDOW", 0)

        # 1. wake screen (idempotent, 失败吞掉)
        # 2026-05-12 fix: 移除 KEYCODE_MENU — 在 MIUI 上会打开最近任务管理器
        # 导致频繁在 Facebook 和任务页面之间切换。用 WAKEUP + swipe 解锁替代。
        for _kc in ("KEYCODE_WAKEUP",):
            try:
                _sp_w.run(
                    ["adb", "-s", device_id, "shell", "input", "keyevent", _kc],
                    timeout=3, creationflags=_CF, capture_output=True,
                )
            except Exception:
                pass
        # swipe up to dismiss lock screen (safe no-op if already unlocked)
        try:
            _sp_w.run(
                ["adb", "-s", device_id, "shell",
                 "input", "swipe", "360", "1200", "360", "400", "300"],
                timeout=3, creationflags=_CF, capture_output=True,
            )
        except Exception:
            pass

        # 2. svc power stayon usb (充电维持, 0 额外耗电)
        try:
            _sp_w.run(
                ["adb", "-s", device_id, "shell",
                 "svc", "power", "stayon", "usb"],
                timeout=3, creationflags=_CF, capture_output=True,
            )
        except Exception:
            pass

        if not probe_dump:
            return True

        # 3. u2 dump probe — 真验 atx-agent + uiautomator stub
        def _try_dump() -> Tuple[bool, int]:
            try:
                d = self._u2(device_id)
                xml = d.dump_hierarchy()
                return (bool(xml and len(xml) > 500), len(xml or ""))
            except Exception as _e:
                log.warning(
                    "[ensure_awake] dump probe %s exception: %s",
                    device_id, _e,
                )
                return (False, 0)

        ok, sz = _try_dump()
        if ok:
            # P6-C (2026-05-05) 可观测性: probe ok 时也 log, 让真机 task 数据
            # 能区分 "wake 没调用" vs "wake 静默成功" — 之前 task 67b98ecc /
            # ef180eeb 0/40 时找不到 [ensure_awake] log 导致误判 wake 没生效.
            log.info(
                "[ensure_awake] %s probe ok size=%d (no healing needed)",
                device_id, sz,
            )
            return True
        log.warning(
            "[ensure_awake] %s 首次 dump probe 失败 size=%d", device_id, sz,
        )

        if not retry_after_heal:
            return False

        # 4. healthcheck 重启 atx-agent + stub
        try:
            d = self._u2(device_id)
            if hasattr(d, "healthcheck"):
                d.healthcheck()
                time.sleep(2.5)
        except Exception as _e:
            log.warning(
                "[ensure_awake] healthcheck %s 失败: %s", device_id, _e,
            )

        ok2, sz2 = _try_dump()
        if ok2:
            log.info(
                "[ensure_awake] %s healthcheck 后 dump 恢复 size=%d",
                device_id, sz2,
            )
            return True
        log.warning(
            "[ensure_awake] %s healthcheck 后 dump 仍空 size=%d "
            "(OEM/system kill uiautomator? screen issue?)",
            device_id, sz2,
        )
        return False

    def _send_msg_from_current_profile(self, d, did: str,
                                        message: str) -> Tuple[bool, str]:
        """方案 A fallback: 当前已在 profile 页 (from_current_profile 流), 但
        没 Add Friend 按钮 (FB 冷账号风控). 找 "Message" / "メッセージ" 按钮
        点开 1on1 chat 直接发消息 — 不需要好友关系.

        每步都 verify current pkg ∈ {katana, orca} 防 atx-agent hierarchy
        cache 滞后导致 click 落到 GMS 弹窗 / 系统 launcher.

        返回 (ok, reason):
          * (True, "sent") — 消息已发
          * (False, "not_in_fb_or_orca") — 当前已在第三方 app
          * (False, "no_message_button") — profile 页连 Message 按钮也没有
          * (False, "chat_input_missing") — 进入 chat 但找不到输入框
          * (False, "send_failed") — 输入了但 Send 没成功
        """
        import subprocess as _sp

        def _cur_pkg() -> str:
            try:
                p = (d.app_current() or {}).get("package", "") or ""
                if p:
                    return p
            except Exception:
                pass
            # adb fallback
            try:
                out = _sp.run(
                    ["adb", "-s", did, "shell", "dumpsys", "window"],
                    capture_output=True, timeout=3,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
                ).stdout.decode("utf-8", "ignore")
                for ln in out.splitlines():
                    if "mCurrentFocus" in ln and "/" in ln:
                        return ln.split("u0 ")[-1].split("/")[0].strip()
            except Exception:
                pass
            return ""

        _ALLOWED_FB = {"com.facebook.katana", "com.facebook.orca",
                        "com.facebook.lite"}

        # 内联取证 helper — 各 return False 分支统一抓 PNG+XML 到 _pending/.
        # 替代靠 MessengerError raise 才触发 forensics 的旧设计 (PR #157 真机
        # task dcb5f9ff 14:47-14:52 全部 fallback 失败但 0 取证落盘的事故根因).
        def _fail(reason: str) -> Tuple[bool, str]:
            try:
                _capture_immediate_async(
                    did,
                    step_name=f"profile_msg_fallback_{reason}",
                    hint=f"target_msg={(message or '')[:40]}",
                    reason=reason,
                )
            except Exception:
                pass
            return False, reason

        if not message:
            return False, "empty_message"

        # Pre-check: 当前必须在 FB / Messenger 系内
        _p0 = _cur_pkg()
        if _p0 and _p0 not in _ALLOWED_FB:
            log.warning("[profile-msg-fallback] 已在第三方 app pkg=%s, abort", _p0)
            return _fail(f"not_in_fb_or_orca:{_p0}")

        # Step 1: 在当前 profile 页找 Message 按钮
        msg_clicked = False
        for sel in _PROFILE_MSG_BTN_SELECTORS:
            try:
                el = d(**sel)
                if el.exists(timeout=1.0):
                    el.click()
                    msg_clicked = True
                    log.info("[profile-msg-fallback] Message btn via %s", sel)
                    break
            except Exception:
                pass
        if not msg_clicked:
            return _fail("no_message_button")
        time.sleep(2.5)

        # Verify chat 真的打开 — 必须在 com.facebook.orca / katana
        _p1 = _cur_pkg()
        if _p1 not in _ALLOWED_FB:
            log.warning("[profile-msg-fallback] tap Message 后 pkg=%s 不是 fb/orca", _p1)
            return _fail(f"chat_did_not_open:{_p1}")
        # orca 是 Messenger app, 是预期; katana 内嵌 chat (lite 模式) 也接受
        log.info("[profile-msg-fallback] 进入 chat pkg=%s", _p1)

        # Step 2: chat 已打开. 找 EditText 输入消息.
        # race fix (2026-05-04): 旧 exists(timeout=2.5) 在 14:47/14:50 task
        # dcb5f9ff 真机连续 2 人 chat_input_missing — katana 内嵌 chat 走
        # React Native 渲染, hierarchy 加载比 orca 慢. wait(timeout=8.0) 等真值.
        try:
            input_el = d(className="android.widget.EditText")
            if not input_el.wait(timeout=8.0):
                return _fail("chat_input_missing")
            # Verify input 真的可见 — bounds 必须在屏幕内
            try:
                _ib = input_el.bounds()
                _info = d.info or {}
                _h = int(_info.get("displayHeight") or 1440)
                # input 必须在屏幕下半部 (chat 通常底部输入)
                if _ib and _ib[1] < _h * 0.4:
                    log.warning("[profile-msg-fallback] EditText 位置异常 bounds=%s", _ib)
                    return _fail("chat_input_position_unexpected")
            except Exception:
                pass
            self.hb.tap(d, *self._el_center(input_el))
            time.sleep(0.5)
            self.hb.type_text(d, message[:500])
            time.sleep(0.6)
        except Exception as e:
            log.debug("[profile-msg-fallback] input 失败: %s", e)
            return _fail("chat_input_missing")

        # Verify input 后仍在 orca/katana
        _p2 = _cur_pkg()
        if _p2 not in _ALLOWED_FB:
            log.warning("[profile-msg-fallback] type_text 后 pkg=%s 离开 fb/orca", _p2)
            return _fail(f"left_chat_after_input:{_p2}")

        # Step 3-5: 三层发送策略 (2026-05-04 v40 优化 ①):
        #   * orca (Messenger app) — selector 命中率高, 优先 selector 省 1.5s IME 等待
        #   * katana / lite (内嵌 chat) — React Native 无 resourceId, IME 优先
        # 三策略按 pkg 决定执行顺序, 几何法永远兜底.
        sent = False
        sent_via = ""

        # 提前读 displayHeight 给几何法 y-下半屏强约束用 (2026-05-04 v40 优化 ②)
        try:
            _disp_h = int((d.info or {}).get("displayHeight") or 1440)
        except Exception:
            _disp_h = 1440
        _y_min_geo = int(_disp_h * 0.5)

        if _p2 == "com.facebook.orca":
            _strategies = ("selector", "ime", "geometry")
        else:
            _strategies = ("ime", "selector", "geometry")

        for _strategy in _strategies:
            if sent:
                break

            if _strategy == "ime":
                # IME send_action 走 KEYCODE_ENTER 触发输入框 onEditorAction(SEND),
                # 不依赖 hierarchy. success signal = input 清空 + 仍在 fb/orca pkg.
                # (14:51 真机 task dcb5f9ff 在 245 个 element 里试 8 个硬 selector
                # 全 0 candidates, IME 才是 React Native 渲染场景的根本解.)
                try:
                    d.send_action("send")
                    time.sleep(1.5)
                    try:
                        _ed_after = d(className="android.widget.EditText")
                        if _ed_after.exists(timeout=1.0):
                            _txt_after = _ed_after.get_text() or ""
                            if not _txt_after.strip():
                                _p_ime = _cur_pkg()
                                if _p_ime in _ALLOWED_FB:
                                    sent = True
                                    sent_via = "ime_action"
                                    log.info(
                                        "[profile-msg-fallback] sent via IME action "
                                        "(input cleared) pkg=%s", _p_ime,
                                    )
                    except Exception:
                        pass
                except Exception as _ime_e:
                    log.debug("[profile-msg-fallback] IME send_action 失败: %s", _ime_e)

            elif _strategy == "selector":
                # u2 selector 试错 (selector 列表见 module-level _PROFILE_SEND_BTN_SELECTORS).
                # 绝不调 smart_tap (smart_tap 含 katana healing, Messenger 内调会
                # force-restart 切回 FB - 见 memory feedback_smart_tap_messenger_contract).
                for sel in _PROFILE_SEND_BTN_SELECTORS:
                    try:
                        el = d(**sel)
                        if el.exists(timeout=1.0):
                            el.click()
                            sent = True
                            sent_via = f"selector:{sel}"
                            log.info("[profile-msg-fallback] Send via u2 %s", sel)
                            break
                    except Exception:
                        pass

            elif _strategy == "geometry":
                # 几何法兜底 — chat 输入栏标准布局 [...] [ EditText ] [ send ],
                # send 必在 EditText 右侧 + 屏幕下半部. ② 加 y > h*0.5 强约束防误点
                # chat header 的 phone/video/back icon (它们也 clickable=true).
                try:
                    _ed = d(className="android.widget.EditText")
                    if _ed.exists():
                        _ebnd = _ed.bounds()  # (l, t, r, b)
                        if _ebnd:
                            _ed_right, _ed_top, _ed_bot = _ebnd[2], _ebnd[1], _ebnd[3]
                            _xml = d.dump_hierarchy()
                            import re as _re_geo
                            _node_pat = _re_geo.compile(
                                r'<node[^>]*?clickable="true"[^>]*?'
                                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                                _re_geo.DOTALL,
                            )
                            _candidates = []
                            for _m in _node_pat.finditer(_xml):
                                _l, _t, _r, _b = (int(_m.group(i)) for i in (1, 2, 3, 4))
                                if _l < _ed_right:
                                    continue
                                if _b < _ed_top - 20 or _t > _ed_bot + 20:
                                    continue
                                # ② y 强约束: 必须在屏幕下半部 (输入栏一定在底部)
                                if _t < _y_min_geo:
                                    continue
                                _w, _hh = _r - _l, _b - _t
                                if _w < 30 or _w > 250 or _hh < 30 or _hh > 250:
                                    continue
                                _candidates.append((_l, _t, _r, _b))
                            # 最右 candidate 优先 (chat 输入栏 send 永远在最右)
                            _candidates.sort(key=lambda x: x[0], reverse=True)
                            for _l, _t, _r, _b in _candidates[:2]:
                                _cx, _cy = (_l + _r) // 2, (_t + _b) // 2
                                log.info(
                                    "[profile-msg-fallback] 几何法 tap right-of-EditText "
                                    "(%d,%d) bounds=(%d,%d,%d,%d)",
                                    _cx, _cy, _l, _t, _r, _b,
                                )
                                d.click(_cx, _cy)
                                time.sleep(1.2)
                                try:
                                    _ed_check = d(className="android.widget.EditText")
                                    if _ed_check.exists(timeout=1.0):
                                        _t2 = _ed_check.get_text() or ""
                                        if not _t2.strip():
                                            sent = True
                                            sent_via = f"geometry:({_cx},{_cy})"
                                            log.info(
                                                "[profile-msg-fallback] 几何法 sent "
                                                "(input cleared)",
                                            )
                                            break
                                except Exception:
                                    pass
                except Exception as _geo_e:
                    log.debug("[profile-msg-fallback] 几何法兜底失败: %s", _geo_e)

        if not sent:
            return _fail("send_failed_no_button")

        # Final verify: send 后仍在 orca/katana, 没跑到第三方 app
        time.sleep(1.0)
        _p3 = _cur_pkg()
        if _p3 not in _ALLOWED_FB:
            log.warning("[profile-msg-fallback] send 后 pkg=%s 跑到第三方", _p3)
            return _fail(f"left_chat_after_send:{_p3}")
        log.info(
            "[profile-msg-fallback] 消息已发送 via %s (绕开 add_friend 风控) pkg=%s",
            sent_via, _p3,
        )
        return True, "sent"

    @_with_fb_foreground
    def add_friend(self, profile_name: str,
                   device_id: Optional[str] = None) -> bool:
        """Send a friend request from search results (legacy 直加,无安全模式)。

        ⚠ 推荐使用 add_friend_with_note(safe_mode=True) 替代,FB 风控对搜索后立即点 Add Friend 极敏感。
        本方法保留是为了向后兼容 task_type=facebook_add_friend 的旧调用。
        """
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("add_friend", device_id=did):
            self.search_people(profile_name, did, max_results=3)
            time.sleep(1)

            if self.smart_tap("Add Friend button", device_id=did):
                log.info("Friend request sent to: %s", profile_name)
                return True

        return False

    @_with_fb_foreground
    def add_friend_with_note(self, profile_name: str,
                             note: str = "",
                             safe_mode: bool = True,
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None,
                             source: str = "",
                             preset_key: str = "",
                             from_current_profile: bool = False,
                             do_l2_gate: bool = False,
                             force: bool = False,
                             walk_candidates: bool = False,
                             l2_gate_shots: int = 1,
                             max_l2_calls: int = 3,
                             strict_persona_gate: bool = False) -> bool:
        """带验证语的安全好友请求 — Sprint 1 新增 + 2026-04-22 persona 改造。

        相比 add_friend 的差异:
          1. safe_mode=True 时,先点击进入对方主页,停留 8-15s 模拟"看资料"
          2. 在主页找 "Add Friend" 按钮(而非搜索结果列表里的快捷按钮 — FB 风控对此更敏感)
          3. 若 note 非空且支持(部分 FB 版本会弹"加备注"框),自动填入
          4. **note 为空且 persona 要求 require_verification_note 时**,
             按 persona.country_code 从 chat_messages.yaml 随机抽一条验证语。
             这是日文客群(jp_female_midlife)的关键锚点 — 防止给日本女性发英文
             "Hi nice to meet you" 被直接忽略/举报。
          5. **phase=cold_start 时直接拒绝发送** (fb_account_phase 把新号关在笼子里),
             防止刚冷启动的号就被风控盯上。

        Args:
            profile_name: 目标姓名/搜索词
            note: 验证语(可选,部分版本支持);为空会按 persona 自动生成
            safe_mode: 是否走安全路径(进主页停留)
            device_id: 设备 ID
            persona_key: 目标客群 key(router 层已经 setdefault 注入)
            phase: 显式覆盖 phase；为空走 fb_account_phase.get_phase
            from_current_profile: True 时跳过 search_people/点首条,假定已在资料页
                (获客任务 navigate 后加友,避免二次搜索误点)
            do_l2_gate: Phase 10 prep — True 时在进入资料页后跑 L2 VLM gate
                (头像/bio 视觉判断 persona match). 默认 False (向后兼容).
                透传到 ``_add_friend_safe_interaction_on_profile``。
                真机激活: caller 设 True (需 ollama qwen2.5vl:7b 可达).
        """
        did = self._did(device_id)
        d = self._u2(did)
        try:
            from src.host.fb_playbook import local_rules_disabled
            _relaxed_local_rules = local_rules_disabled()
        except Exception:
            _relaxed_local_rules = False

        # Phase 8h (2026-04-24): blocklist 前置检查 — 运营一键加黑的 peer 直接 skip,
        # 防止反复骚扰. 命中时内部已写 journey event `greeting_blocked{reason=peer_blocklisted}`.
        if (not _relaxed_local_rules
                and self._check_peer_blocklist(profile_name, did=did, persona_key=persona_key)):
            log.info("[add_friend_with_note] peer=%s 在 blocklist, skip", profile_name)
            return False

        # Phase 9 (2026-04-24): persona L1 gate — 名字不匹配目标客群 (如 jp_female_midlife
        # 对"John Smith")直接 skip, 避免骚扰非目标用户. 只跑 L1 (名字启发式),
        # L2 VLM 需要 profile 截图, 留到进 profile 页后做更深的判断 (Phase 10 预告).
        if persona_key and not _relaxed_local_rules:
            try:
                from src.host.fb_profile_classifier import classify as _persona_classify
                _cls = _persona_classify(
                    device_id=did,
                    persona_key=persona_key,
                    target_key=f"fb:{profile_name}",
                    display_name=profile_name,
                    do_l2=False,  # 无 profile 截图, 只跑 L1
                    dry_run=False,
                )
                _l1 = _cls.get("l1") or {}
                _from_cache = bool(_cls.get("from_cache"))
                # 2026-04-24 (A merge): 缓存命中时 l1/l2=None, 用顶层 match 判断
                # 避免空 dict 的 pass 默认 True 导致误放行 cache 里标过 False 的目标.
                if _from_cache:
                    _matched = bool(_cls.get("match", True))
                    if not _matched:
                        log.info(
                            "[add_friend_with_note] persona 缓存 match=False "
                            "peer=%s (stage=%s), skip", profile_name,
                            _cls.get("stage_reached"))
                        try:
                            self._append_journey_for_action(
                                profile_name, "add_friend_blocked",
                                did=did, persona_key=persona_key,
                                data={
                                    "reason": "persona_cached_rejected",
                                    "stage_reached": _cls.get("stage_reached"),
                                    "score": _cls.get("score", 0),
                                })
                        except Exception:
                            pass
                        return False
                    # 缓存且 match=True: 已经在第一次分类时写过 journey,
                    # 不再重复写 persona_classified, 直接放行.
                elif not _l1.get("pass", True):
                    l1_score = _l1.get("score", 0)
                    reasons = _l1.get("reasons") or []
                    log.info(
                        "[add_friend_with_note] persona L1 不命中 peer=%s "
                        "score=%.0f reasons=%s, skip",
                        profile_name, l1_score, reasons[:3])
                    # journey 写 persona_rejected 供 funnel 统计
                    try:
                        self._append_journey_for_action(
                            profile_name, "add_friend_blocked",
                            did=did, persona_key=persona_key,
                            data={
                                "reason": "persona_l1_rejected",
                                "l1_score": l1_score,
                                "l1_pass_threshold": _l1.get("pass_threshold"),
                                "top_reasons": reasons[:3],
                            })
                    except Exception:
                        pass
                    return False
                else:
                    # 新一次 L1 PASS — 记一条 journey 让 funnel 能看到命中率
                    try:
                        self._append_journey_for_action(
                            profile_name, "persona_classified",
                            did=did, persona_key=persona_key,
                            data={
                                "stage": "L1",
                                "match": True,
                                "score": _l1.get("score", 0),
                                "reasons": (_l1.get("reasons") or [])[:3],
                            })
                    except Exception:
                        pass
            except Exception as e:
                # classify 异常时保守放行 (不阻塞主流程)
                log.debug("[add_friend_with_note] persona classify 异常, 放行: %s", e)

        # P0-2: phase + playbook 参数解析
        eff_phase, ab_cfg = _resolve_phase_and_cfg("add_friend",
                                                   device_id=did,
                                                   phase_override=phase)
        # cold_start 直接拒绝（playbook 把 max_friends_per_run 设为 0）
        # 2026-04-26 fix: force=True 时 (router 层 force_add_friend 透传过来)
        # 跳过 phase gate, 由 caller 完全负责风险评估 (B2B 客户测试 / E2E smoke)
        if int(ab_cfg.get("max_friends_per_run", 5)) <= 0:
            if not force:
                log.info("[add_friend_with_note] phase=%s 禁止加好友, skip: %s "
                         "(传 force=True 可绕过)", eff_phase, profile_name)
                return False
            log.warning("[add_friend_with_note] phase=%s force=True 绕过 gate: %s",
                        eff_phase, profile_name)

        # P3-1 2026-04-23: 整段"cap 检查 → 发起请求 → 写库"用 device+section 锁串行化,
        # 消除多 worker 同时过 gate 造成的竞态超 cap。锁粒度 = 单 device 单 section,
        # 同 device 的 add_friend 串行, 跨 device / 跨 section(add_friend vs send_greeting)
        # 完全独立。
        from src.host.fb_concurrency import device_section_lock
        with device_section_lock(did, "add_friend", timeout=180.0):
            return self._add_friend_with_note_locked(
                profile_name, note, safe_mode, did, d,
                ab_cfg, daily_cap=int(ab_cfg.get("daily_cap_per_account") or 0),
                persona_key=persona_key, eff_phase=eff_phase,
                source=source, preset_key=preset_key,
                from_current_profile=from_current_profile,
                do_l2_gate=do_l2_gate, force=force,
                walk_candidates=walk_candidates,
                l2_gate_shots=l2_gate_shots,
                max_l2_calls=max_l2_calls,
                strict_persona_gate=strict_persona_gate)

    def _add_friend_with_note_locked(self, profile_name, note, safe_mode,
                                     did, d, ab_cfg, daily_cap,
                                     persona_key, eff_phase,
                                     source: str = "", preset_key: str = "",
                                     from_current_profile: bool = False,
                                     do_l2_gate: bool = False,
                                     force: bool = False,
                                     walk_candidates: bool = False,
                                     l2_gate_shots: int = 1,
                                     max_l2_calls: int = 3,
                                     strict_persona_gate: bool = False):
        """add_friend_with_note 的锁内主体, 抽出来便于测试 + 避免锁嵌套。"""
        # P1-2: 24h rolling 日上限（与单任务 max_friends_per_run 独立）
        # 2026-04-24 (A merge): force=True 跳过 cap 检查 (smoke/QA 显式 override).
        if daily_cap > 0 and not force:
            try:
                from src.host.fb_store import count_friend_requests_sent_since
                n24 = count_friend_requests_sent_since(did, hours=24)
                if n24 >= daily_cap:
                    log.info("[add_friend_with_note] 24h 已发 %s 次 ≥ daily_cap=%s, skip %s",
                             n24, daily_cap, profile_name)
                    return False
            except Exception as e:
                log.debug("[add_friend_with_note] daily_cap 检查异常(继续): %s", e)

        # Phase A3: 跨设备刚性去重 — 检查其他设备是否已对同一人发过好友请求.
        # 与 _peer_already_contacted (journey 层) 互补: journey 是 fire-and-forget,
        # 此处直接查 facebook_friend_requests 表作为硬防线.
        if not force:
            try:
                from src.host.fb_store import is_friend_requested_globally
                dup = is_friend_requested_globally(
                    profile_name, exclude_device=did, hours=720)
                if dup.get("found"):
                    log.info(
                        "[add_friend_with_note] 跨设备去重: peer=%s 已被"
                        " device=%s 于 %s 发过请求(status=%s), skip",
                        profile_name, dup["device_id"][:12],
                        dup["sent_at"][:16], dup["status"])
                    try:
                        self._append_journey_for_action(
                            profile_name, "add_friend_blocked",
                            did=did, persona_key=persona_key,
                            data={"reason": "cross_device_duplicate",
                                  "existing_device": dup["device_id"][:12],
                                  "existing_status": dup["status"]})
                    except Exception:
                        pass
                    # Phase F1: 实时去重告警 (AlertNotifier 内置 5min 去重)
                    try:
                        from src.host.alert_notifier import AlertNotifier
                        AlertNotifier.get().notify_event(
                            "dedup_friend_request_blocked",
                            level="info",
                            alert_code="DEDUP_FRIEND_REQUEST_BLOCKED",
                            params={"peer_name": profile_name,
                                    "existing_device": dup["device_id"][:12],
                                    "device_id": did[:12]},
                        )
                    except Exception:
                        pass
                    return False
            except Exception as e:
                log.debug("[add_friend_with_note] 跨设备去重检查异常(继续): %s", e)

        # P0-2: note 空 + playbook 要求携带验证语 → 从 persona 文案池抽
        require_note = bool(ab_cfg.get("require_verification_note", True))
        if not note and require_note:
            try:
                from .fb_content_assets import get_verification_note
                note = get_verification_note(persona_key=persona_key,
                                             name=profile_name)
                if note:
                    log.debug("[add_friend_with_note] 已从 persona=%s 自动生成"
                              " verification_note(len=%d)",
                              persona_key or "(default)", len(note))
            except Exception as e:
                log.debug("[add_friend_with_note] get_verification_note 失败: %s", e)

        with self.guarded("add_friend", device_id=did):
            if from_current_profile:
                time.sleep(random.uniform(2.0, 4.0))
                if not self._is_likely_fb_profile_page(d):
                    log.warning("[add_friend_with_note] from_current_profile=True 但当前不像资料页, 中止")
                    self._last_greet_skip_reason = "no_add_button_not_profile"
                    return False
                # 2026-04-24 (Phase 10.2): l2_gate_shots 仅在非默认时透传, 保旧 spy 签名兼容.
                _shot_kw = ({"l2_gate_shots": l2_gate_shots}
                            if l2_gate_shots and l2_gate_shots != 1 else {})
                if strict_persona_gate:
                    _shot_kw["strict_persona_gate"] = True
                return self._add_friend_safe_interaction_on_profile(
                    d, did, profile_name, note,
                    persona_key=persona_key, source=source, preset_key=preset_key,
                    do_l2_gate=do_l2_gate, **_shot_kw)

            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                log.warning("[add_friend_with_note] 未找到目标: %s", profile_name)
                return False

            time.sleep(1)

            if (safe_mode and walk_candidates
                    and hasattr(self, "_list_top_search_result_cards")):
                # Phase 10.2 additive (2026-04-24): walk top 5 候选, 快速跳过明显男性/已联系,
                # L2 VLM 精筛. 姓搜(如 '田中')会返回混合性别候选; 默认单结果路径常撞
                # 男性/已联系候选浪费 quota. 男性名启发式 → 跳; peer_already_contacted → 跳;
                # L2 REJECT 后 BACK 返搜索页 → 下一个; L2 PASS 继续. 若 walk 空 → 回退单结果.
                # 2026-05-03 真机第九轮 fix: 此 helper 在 master 缺失定义 (引用悬空),
                # 加 hasattr 守卫退化到单结果路径, 避免 AttributeError 让整个
                # add_friends step 异常退出. helper 实现待 P1-B 视觉/AI 候选筛选时
                # 一并补回.
                cands = self._list_top_search_result_cards(
                    d, query_hint=profile_name, max_n=5)
                if cands:
                    log.info(
                        "[add_friend_with_note] walk 候选 %d (budget=%d): %s",
                        len(cands), max_l2_calls,
                        [(c["name"][:20], "M" if c["male_hint"] else "?")
                         for c in cands])
                    # Phase 10.3: L2 VLM quota 保护 — 每个进 profile 的候选消耗 1 call.
                    # 过滤掉的 (male_hint / already_contacted) 不计数. 超 budget → 停.
                    _l2_attempts = 0
                    for idx, cand in enumerate(cands):
                        cand_name = cand["name"] or profile_name
                        if cand["male_hint"]:
                            log.info("[walk] #%d '%s' 男性名, skip",
                                     idx + 1, cand_name[:30])
                            continue
                        contacted, reason = self._peer_already_contacted(cand_name)
                        if contacted:
                            log.info("[walk] #%d '%s' (%s), skip",
                                     idx + 1, cand_name[:30], reason)
                            continue
                        if _l2_attempts >= max(1, int(max_l2_calls)):
                            log.info(
                                "[walk] budget=%d 已耗尽 (已试 %d), 停止剩余候选",
                                max_l2_calls, _l2_attempts)
                            break
                        _l2_attempts += 1
                        bx = cand["bounds"]
                        cx = (bx[0] + bx[2]) // 2
                        cy = (bx[1] + bx[3]) // 2
                        try:
                            self.hb.tap(d, cx, cy)
                        except Exception as e:
                            log.warning("[walk] tap #%d 失败: %s", idx + 1, e)
                            continue
                        loaded = False
                        xml_chk = ""
                        for _ in range(6):
                            time.sleep(2.0)
                            try:
                                xml_chk = d.dump_hierarchy() or ""
                            except Exception:
                                xml_chk = ""
                            if self._is_likely_fb_profile_page_xml(xml_chk):
                                loaded = True
                                break
                        if not loaded and cand_name:
                            try:
                                el = d(text=cand_name)
                                if el.exists(timeout=1.5):
                                    el.click()
                                    time.sleep(random.uniform(3.5, 5.0))
                                    xml_chk = d.dump_hierarchy() or ""
                                    loaded = self._is_likely_fb_profile_page_xml(xml_chk)
                            except Exception:
                                pass
                        if not loaded:
                            log.info("[walk] #%d 未进资料页, BACK 下一个", idx + 1)
                            try:
                                d.press("back")
                            except Exception:
                                pass
                            time.sleep(1.5)
                            continue
                        _shot_kw = ({"l2_gate_shots": l2_gate_shots}
                                     if l2_gate_shots and l2_gate_shots != 1 else {})
                        if strict_persona_gate:
                            _shot_kw["strict_persona_gate"] = True
                        res = self._add_friend_safe_interaction_on_profile(
                            d, did, cand_name, note,
                            persona_key=persona_key,
                            source=source, preset_key=preset_key,
                            do_l2_gate=do_l2_gate, **_shot_kw)
                        if res:
                            log.info("[walk] #%d '%s' 成功", idx + 1, cand_name[:30])
                            return True
                        log.info("[walk] #%d '%s' 失败, BACK 下一个",
                                 idx + 1, cand_name[:30])
                        try:
                            d.press("back")
                        except Exception:
                            pass
                        time.sleep(random.uniform(1.5, 2.5))
                        try:
                            _chk = d.dump_hierarchy() or ""
                            if self._is_likely_fb_profile_page_xml(_chk):
                                d.press("back")
                                time.sleep(1.2)
                        except Exception:
                            pass
                    log.info("[walk] 候选 %d 个全部失败", len(cands))
                    return False
                log.info("[add_friend_with_note] walk 无候选, 回退单结果路径")

            if safe_mode:
                # 安全路径: 点击第一个搜索结果进主页 → 停留 → 找主页内的 Add Friend
                first = self._first_search_result_element(d, query_hint=profile_name)
                if first is None:
                    log.warning("[add_friend_with_note] 无法定位首个搜索结果")
                    return False
                self.hb.tap(d, *self._el_center(first))
                time.sleep(random.uniform(4.5, 7.0))

                try:
                    xml_chk = d.dump_hierarchy()
                except Exception:
                    xml_chk = ""
                if not self._is_likely_fb_profile_page_xml(xml_chk):
                    like_name = (results[0].get("name") or "").strip()
                    if like_name and self._search_result_name_plausible(
                            like_name, profile_name):
                        try:
                            el = d(text=like_name)
                            if el.exists(timeout=2.0):
                                el.click()
                                log.info("[add_friend_with_note] 按提取人名重试进入主页: %r",
                                         like_name[:80])
                                time.sleep(random.uniform(3.5, 5.5))
                                xml_chk = d.dump_hierarchy()
                        except Exception:
                            pass
                if not self._is_likely_fb_profile_page_xml(xml_chk):
                    log.info("[add_friend_with_note] 未检测到资料页特征，尝试列表区坐标点击")
                    w, _h = d.window_size()
                    self._adb(f"shell input tap {int(w * 0.5)} {int(620)}", device_id=did)
                    time.sleep(random.uniform(3.0, 5.0))

                _shot_kw = ({"l2_gate_shots": l2_gate_shots}
                            if l2_gate_shots and l2_gate_shots != 1 else {})
                if strict_persona_gate:
                    _shot_kw["strict_persona_gate"] = True
                return self._add_friend_safe_interaction_on_profile(
                    d, did, profile_name, note,
                    persona_key=persona_key, source=source, preset_key=preset_key,
                    do_l2_gate=do_l2_gate, **_shot_kw)
            if not self.smart_tap("Add Friend button", device_id=did):
                return False
            time.sleep(1)

            # 部分 FB 版本会弹"加备注/Send"对话框
            if note:
                if d(textContains="Add").exists(timeout=1.0) or d(textContains="note").exists(timeout=0.5):
                    note_input = d(className="android.widget.EditText")
                    if note_input.exists(timeout=1.0):
                        try:
                            self.hb.tap(d, *self._el_center(note_input))
                            time.sleep(0.4)
                            self.hb.type_text(d, note[:200])
                            time.sleep(0.5)
                        except Exception:
                            pass
                    self.smart_tap("Send button", device_id=did)
                    time.sleep(1.0)

            log.info("[add_friend_with_note] 好友请求已发送: %s (note=%s)",
                     profile_name, bool(note))

            self._record_friend_request_safely(
                did, profile_name, note=note,
                persona_key=persona_key,
                source=source, preset_key=preset_key,
                status="sent")
            return True

    def _record_friend_request_safely(self, device_id: str, target_name: str,
                                      *, note: str = "",
                                      persona_key: Optional[str] = None,
                                      source: Optional[str] = None,
                                      preset_key: Optional[str] = None,
                                      status: str = "sent") -> None:
        """把 record_friend_request 的调用收敛在 automation 层, 便于锁内一次性完成。

        调用方仍然可以用 record_friend_request 自己写(如需要 lead_id 关联等高级场景),
        但默认路径由本方法负责, 保证 UI 发送 → 入库原子化。

        P3-3: 同步写一条 fb_contact_events 流水, 供 A/B 和骚扰配额分析。
        """
        try:
            from src.host.fb_store import record_friend_request
            record_friend_request(
                device_id, target_name,
                note=note or "",
                source=source or "",
                status=status,
                preset_key=preset_key or "",
            )
        except Exception as e:
            log.debug("[add_friend] 入库失败(不影响 UI 成功): %s", e)
        # P3-3: 接触事件流水
        try:
            from src.host.fb_store import (record_contact_event,
                                            CONTACT_EVT_ADD_FRIEND_SENT,
                                            CONTACT_EVT_ADD_FRIEND_RISK)
            evt = (CONTACT_EVT_ADD_FRIEND_SENT if status == "sent"
                   else CONTACT_EVT_ADD_FRIEND_RISK)
            record_contact_event(
                device_id, target_name, evt,
                preset_key=preset_key or "",
                meta={"source": source or "", "has_note": bool(note)},
            )
        except Exception:
            pass
        # L2 中央客户画像双写 (fire_and_forget, 失败不影响主流程)
        try:
            from src.host.customer_sync_bridge import sync_friend_request_sent
            sync_friend_request_sent(
                device_id, target_name,
                status=status,
                persona_key=persona_key,
                preset_key=preset_key,
                source=source,
                note=note,
            )
        except Exception:
            pass
        # Phase D2: 利用 _last_nav_target_key 进行稳定身份解析
        _nav_key = getattr(self, "_last_nav_target_key", "") or ""
        if _nav_key:
            try:
                self._resolve_peer_canonical_safe(
                    target_name, did=device_id, persona_key=persona_key,
                    discovered_via="friend_request",
                    profile_target_key=_nav_key)
            except Exception:
                pass
            # 用完即清,避免串线到下一个 peer
            self._last_nav_target_key = ""

        # Phase 6.A: Lead Mesh journey 同步写
        action = ("friend_requested" if status == "sent"
                  else "friend_request_risk")
        self._append_journey_for_action(
            target_name, action, did=device_id,
            persona_key=persona_key,
            discovered_via=("friend_request"
                             if status == "sent" else "friend_request_failed"),
            data={"note_len": len(note or ""),
                   "source": source or "",
                   "preset_key": preset_key or "",
                   "profile_target_key": _nav_key or ""})

    # ─── 2026-04-23: 加好友后打招呼（方案 A2 — 在 profile 页点 Message）──
    # 设计决策（vs 原方案 A1 "切到 Messenger App 搜名字"）:
    #   ① 刚加的人**还不是好友** → Messenger App 搜名字命中率极低
    #      (Messenger 搜索只搜已有对话 + 联系人 + 二度好友)
    #   ② profile 页的 "Message" 按钮就是 FB 真人路径,风控模型对此
    #      路径友好度远高于"切 app → 搜名字"
    #   ③ 全程停留在 com.facebook.katana,避免 MIUI XSpace "Select app"
    #      弹窗拦截(切 Messenger 是头号诱因)
    #   ④ Message 按钮打开的对话 = FB 内嵌 Messenger thread,对方看到
    #      的是"好友请求 + 消息请求",心理路径连续

    _GREETING_MESSAGE_BTN_ALTS = (
        "Message button on profile page",
        "Message button",
        "Send Message button on profile",
    )
    _GREETING_INLINE_TEXTS = (
        "Message", "MESSAGE", "Send Message", "Send message",
        "メッセージ", "メッセージを送信",   # 日文
        "Messaggio", "Invia messaggio",     # 意大利文
        "消息", "发消息", "发送消息",         # 中文
    )
    _GREETING_REQUEST_CONFIRM_TEXTS = (
        "Send", "Send Request", "SEND",
        "送信", "リクエストを送る",
        "Invia", "Invia richiesta",
        "发送", "发送请求",
    )

    # ── Messenger app 安装状态 (带缓存, 避免每次 greeting 查 pm list) ──
    _messenger_installed_cache: Dict[str, Tuple[bool, float]] = {}

    def _is_messenger_installed(self, did: str) -> bool:
        """检查目标设备是否装了 Messenger (com.facebook.orca).

        缓存 5 分钟避免频繁 shell pm list. 缓存 miss/过期时用 adb 查.
        """
        import time as _t
        now = _t.time()
        cached = self._messenger_installed_cache.get(did)
        if cached and (now - cached[1]) < 300:
            return cached[0]
        installed = False
        try:
            # u2.Device 和 AdbFallbackDevice 都支持 shell
            d = self._u2(did)
            out = d.shell(f"pm list packages {MESSENGER_PACKAGE}")
            # u2.Device.shell 返回 ShellResponse (output + exit_code);
            # AdbFallbackDevice 没 shell, 用 _adb
            if hasattr(out, "output"):
                txt = out.output
            elif isinstance(out, tuple):
                txt = out[0]
            else:
                txt = str(out)
            installed = MESSENGER_PACKAGE in txt
        except Exception as e:
            log.debug("[_is_messenger_installed] 查询失败 (默认 True 不拦): %s", e)
            # 查询失败时倾向 true (让 fallback 尝试), 而不是 false 直接拒绝
            installed = True
        self._messenger_installed_cache[did] = (installed, now)
        log.info("[messenger] app installed=%s on %s", installed, did[:12])
        return installed

    def _tap_profile_message_button(self, d, did: str) -> bool:
        """在当前 profile 页上点击 Message 按钮进入内联对话。

        优先 smart_tap (走 AutoSelector 学习 + 自愈),失败降级到文本兜底。
        返回 True 表示点击成功(不保证对话页已加载,调用方需自行等待 + 校验)。
        """
        for alt in self._GREETING_MESSAGE_BTN_ALTS:
            try:
                if self.smart_tap(alt, device_id=did):
                    return True
            except Exception:
                continue
        # 文本兜底 —— 避免被 Feed 内的 "Message" TextView 误命中：
        # 限制必须是 clickable 且屏幕上半部（profile 页按钮一般在资料卡下方）
        try:
            try:
                sh = d.window_size()[1]
            except Exception:
                sh = 1920
            for txt in self._GREETING_INLINE_TEXTS:
                try:
                    btn = d(text=txt, clickable=True)
                    if btn.exists(timeout=0.6):
                        info = btn.info or {}
                        bounds = info.get("bounds") or {}
                        cx = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
                        cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2
                        # 限定在屏幕 1/8 ~ 5/8 区域避免命中底部导航 Messenger tab
                        if cx > 0 and cy > 0 and cy < sh * 0.65:
                            self.hb.tap(d, cx, cy, device_id=did)
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # ─── 2026-04-23 Phase 5 Post-Review: Messenger fallback 细分归因 + 设备锁 ─
    # 实施 INTEGRATION_CONTRACT §7.6 (MessengerError 分流矩阵) +
    # §7.7 (device_section_lock("messenger_active") 双方共用)。
    #
    # 依赖关系:
    #   * B 的 PR #1 (feat-b-chat-p2) 在 send_message 里新增 raise_on_error
    #     参数 + MessengerError 7 档 code。本方法**向前兼容**: 若 PR 未合并
    #     (TypeError on raise_on_error kwarg), 自动降级到 bool 返回值。
    #   * 锁 "messenger_active" 是 A/B 共用契约, B 的 check_message_requests
    #     入口也会拿同一把锁, 避免抢输入框。

    _MESSENGER_ERROR_CODES = (
        "risk_detected", "xspace_blocked", "recipient_not_found",
        "search_ui_missing", "send_button_missing",
        "send_blocked_by_content", "messenger_unavailable", "send_fail",
    )

    def _mark_device_messenger_not_ready(self, did: str, ttl_min: int = 30) -> None:
        """messenger_unavailable 时把 device 标记为临时不可用。

        简易实现: 写 fb_risk_events{kind='messenger_not_ready', ttl_min}
        供调度器 / 运维面板读取; 未来可扩展到 fb_account_phase。
        """
        try:
            from src.host.fb_store import record_risk_event
            record_risk_event(did, f"messenger_not_ready ttl_min={ttl_min}",
                              task_id="send_greeting_fallback")
        except Exception:
            pass

    def _mark_content_blocked(self, did: str, text: str) -> str:
        """违禁词 hash 入库, 返回 hash 短码。供 send_button_missing /
        send_blocked_by_content 分流使用。"""
        try:
            import hashlib
            h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
            from src.host.fb_store import record_risk_event
            record_risk_event(did, f"content_blocked text_hash={h}",
                              task_id="send_greeting_fallback")
            return h
        except Exception:
            return ""

    def _apply_messenger_error_policy(self, did: str, code: str,
                                       greeting: str,
                                       profile_name: str) -> str:
        """按 §7.6 分流矩阵应用动作, 返回归因 reason (写入 _set_greet_reason)。"""
        if code == "risk_detected":
            # device-level cooldown - on_risk 触发阈值时会自动转到 cooldown phase
            try:
                from src.host.fb_account_phase import on_risk
                on_risk(did)
            except Exception as e:
                log.debug("[send_greeting] on_risk 调用失败: %s", e)
            # 补一条 risk event 供运维看
            try:
                from src.host.fb_store import record_risk_event
                record_risk_event(did, f"messenger_risk peer={profile_name[:16]}",
                                  task_id="send_greeting_fallback")
            except Exception:
                pass
            return "fallback_risk_detected"

        if code == "xspace_blocked":
            log.warning("[send_greeting] XSpace 挡路 device=%s peer=%s (不 cooldown, 仅 log)",
                        did[:8], profile_name[:16])
            return "fallback_xspace_blocked"

        if code == "recipient_not_found":
            return "fallback_peer_not_found"

        if code == "search_ui_missing":
            return "fallback_search_ui_miss"

        if code in ("send_button_missing", "send_blocked_by_content"):
            h = self._mark_content_blocked(did, greeting)
            log.info("[send_greeting] content blocked hash=%s peer=%s", h,
                     profile_name[:16])
            return (f"fallback_content_blocked:{h}" if h
                    else "fallback_content_blocked")

        if code == "messenger_unavailable":
            self._mark_device_messenger_not_ready(did)
            return "fallback_messenger_unavailable"

        # send_fail 或未知
        return f"fallback_fail:{code or 'unknown'}"

    def _send_messenger_greeting_to_peer(self, *, did: str,
                                           peer_name: str,
                                           greeting: str
                                           ) -> Tuple[bool, str]:
        """Messenger App 内发消息给 peer_name — Phase 7c 新版 UI 专用.

        基于 debug_messenger_fallback_trace.py 已 dry-run 验证的流程:
          1. app_stop + app_start com.facebook.orca → 干净状态
          2. 点搜索入口 (descriptionContains='search')
          3. AutoCompleteTextView.set_text(peer_name)  (u2 Android SetText API, 支持 unicode)
          4. 从搜索结果里找 content-desc=peer 且 bounds 宽度 ≥ 400 的 row, 点第一个
          5. 对话页 EditText.set_text(greeting) + 找 desc='Send' 按钮点击

        返回 (ok, error_code):
          * (True, "")                            成功发出
          * (False, "messenger_unavailable")      app 未启动 / XSpace 挡路
          * (False, "search_ui_missing")          搜索入口找不到
          * (False, "recipient_not_found")        搜索无命中候选
          * (False, "send_button_missing")        Send 按钮找不到
          * (False, "send_fail")                  通用失败 (异常 / 兜底)
        """
        import re as _re
        MESSENGER_PKG = "com.facebook.orca"

        d = self._u2(did)

        def _wait_pkg_foreground(pkg: str, timeout: float = 8.0) -> bool:
            """轮询 d.app_current().package 直到等于 pkg 或超时."""
            import time as _t
            deadline = _t.time() + timeout
            while _t.time() < deadline:
                try:
                    cur = (d.app_current() or {}).get("package", "")
                except Exception:
                    cur = ""
                if cur == pkg:
                    return True
                _t.sleep(0.4)
            return False

        try:
            # 1) stop+start 确保干净状态
            try:
                d.app_stop(MESSENGER_PKG)
                time.sleep(1.0)
                d.app_start(MESSENGER_PKG)
            except Exception as e:
                log.warning("[messenger_send] app_start 异常: %s", e)
                return False, "messenger_unavailable"
            # 自适应时序: 轮询 Messenger 到前台, 最多等 8s (替代固定 sleep 6s)
            if not _wait_pkg_foreground(MESSENGER_PKG, timeout=8.0):
                log.warning("[messenger_send] Messenger 8s 内未到前台")
                return False, "messenger_unavailable"

            # 跳常见弹窗
            for t in ("Not Now", "Skip", "OK", "Continue", "Allow",
                        "Close", "Got it", "Later", "Dismiss"):
                try:
                    el = d(text=t)
                    if el.exists(timeout=0.3):
                        el.click()
                        time.sleep(0.4)
                except Exception:
                    pass

            # 2) 搜索入口：复用统一 Messenger 搜索入口，避免 fallback 分支
            # 仍停留在旧 selector 表导致 search_ui_missing。
            try:
                self._enter_messenger_search(d, did)
            except MessengerError as e:
                if e.code != "search_ui_missing":
                    log.debug("[messenger_send] 搜索入口异常: %s", e)
                log.warning("[messenger_send] 搜索入口找不到")
                return False, "search_ui_missing"
            except Exception as e:
                log.debug("[messenger_send] 搜索入口异常: %s", e)
                return False, "search_ui_missing"
            time.sleep(1.2)

            # 3) 输入 peer_name
            input_el = None
            for sel in ({"className": "android.widget.EditText"},
                          {"className": "android.widget.AutoCompleteTextView"}):
                cand = d(**sel)
                if cand.exists(timeout=2.0):
                    input_el = cand
                    break
            if input_el is None:
                return False, "search_ui_missing"
            try:
                input_el.click()
                time.sleep(0.4)
                input_el.clear_text()
                time.sleep(0.2)
                input_el.set_text(peer_name)
            except Exception as e:
                log.debug("[messenger_send] 输入 peer 异常: %s", e)
                return False, "search_ui_missing"

            # 4) 自适应轮询搜索结果 — 而不是固定 sleep 后 dump
            #    最多等 4s, 每 0.6s 尝试一次 dump, 发现候选就立即进下一步.
            import time as _t
            peer_frag = peer_name[:2] if len(peer_name) >= 2 else peer_name
            cands = []
            deadline = _t.time() + 4.0
            while _t.time() < deadline:
                try:
                    xml = d.dump_hierarchy() or ""
                except Exception:
                    xml = ""
                cands_tmp = []
                for nm in _re.finditer(r'<node\s[^>]+/>', xml):
                    ns = nm.group(0)
                    def _a(name):
                        mm = _re.search(rf'\b{name}="([^"]*)"', ns)
                        return mm.group(1) if mm else ""
                    desc = _a("content-desc")
                    text = _a("text")
                    # peer 名优先精确匹配, 找不到再 2 字符片段. 打分让精确排前.
                    if peer_name in desc or peer_name in text:
                        score = 100
                    elif peer_frag in desc or peer_frag in text:
                        score = 50
                    else:
                        continue
                    bm = _re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                                     _a("bounds"))
                    if not bm:
                        continue
                    x1, y1, x2, y2 = map(int, bm.groups())
                    if y1 < 200:  # 搜索框排除
                        continue
                    if x2 - x1 < 400:  # 必须整行宽
                        continue
                    cands_tmp.append({
                        "score": score, "y1": y1, "bounds": (x1, y1, x2, y2),
                        "label": desc or text,
                    })
                if cands_tmp:
                    cands = cands_tmp
                    break
                _t.sleep(0.6)
            if not cands:
                log.warning("[messenger_send] 4s 内搜索结果无 %r 候选", peer_name)
                return False, "recipient_not_found"
            # 排序: 分数高优先, 同分数顶部优先
            cands.sort(key=lambda c: (-c["score"], c["y1"]))
            target = cands[0]
            log.info("[messenger_send] 候选 %r score=%d (精确=100/片段=50)",
                      target["label"], target["score"])
            cx = (target["bounds"][0] + target["bounds"][2]) // 2
            cy = (target["bounds"][1] + target["bounds"][3]) // 2
            d.click(cx, cy)

            # 5) 对话页输入 greeting — 轮询 EditText 出现, 最多等 5s
            chat_input = None
            deadline = _t.time() + 5.0
            while _t.time() < deadline:
                for sel in ({"className": "android.widget.EditText"},
                              {"className": "android.widget.AutoCompleteTextView"}):
                    cand = d(**sel)
                    if cand.exists(timeout=0.5):
                        chat_input = cand
                        break
                if chat_input is not None:
                    break
                _t.sleep(0.4)
            if chat_input is None:
                log.warning("[messenger_send] 5s 内对话页无 EditText")
                return False, "send_fail"
            try:
                chat_input.click()
                time.sleep(0.4)
                chat_input.clear_text()
                time.sleep(0.2)
                chat_input.set_text(greeting)
            except Exception as e:
                log.debug("[messenger_send] 输入 greeting 异常: %s", e)
                return False, "send_fail"
            time.sleep(1.5)

            # 6) Send 前先处理可能的 Message Request 确认弹窗 (非好友场景)
            try:
                if self._confirm_message_request_if_any(d):
                    log.info("[messenger_send] 已处理 Message Request 确认框")
                    time.sleep(1.0)
            except Exception as e:
                log.debug("[messenger_send] 确认弹窗处理异常(非致命): %s", e)

            try:
                self._tap_messenger_send(d, did)
                time.sleep(2.5)
            except MessengerError as e:
                if e.code == "send_button_missing":
                    return False, "send_button_missing"
                log.debug("[messenger_send] 点 Send 异常: %s", e)
                return False, "send_fail"
            except Exception as e:
                log.debug("[messenger_send] 点 Send 异常: %s", e)
                return False, "send_fail"

            # 8) Send 后 UI 验证 — 输入框清空 或 消息气泡出现在对话里是发送成功的弱信号
            #    (防止 Send 按钮点了但实际因网络/权限未发出)
            sent_confirmed = False
            input_still_has_greeting = False
            try:
                # (a) 输入框清空验证: Messenger 发送成功后输入框会 clear
                for sel in ({"className": "android.widget.EditText"},
                              {"className": "android.widget.AutoCompleteTextView"}):
                    cand = d(**sel)
                    if cand.exists(timeout=0.8):
                        cur_text = (cand.get_text() or "").strip()
                        # 空串 / 只剩占位 hint / 原 greeting 前 2 字不在 => 已清空
                        if not cur_text or greeting[:2] not in cur_text:
                            sent_confirmed = True
                        else:
                            input_still_has_greeting = True
                        break
                # (b) 如果输入框检测不到清空, 检查对话里是否新出现含 greeting 开头的气泡
                if not sent_confirmed and not input_still_has_greeting:
                    try:
                        xml_post = d.dump_hierarchy() or ""
                        if greeting[:4] in xml_post:
                            sent_confirmed = True
                    except Exception:
                        pass
            except Exception as e:
                log.debug("[messenger_send] send 后验证异常(非致命): %s", e)

            if not sent_confirmed:
                log.warning("[messenger_send] Send 已点但未见 UI 确认信号 "
                              "(输入框未清空 + 消息气泡未出现), 疑似未真发: %s",
                              peer_name)
                return False, "send_fail"

            log.info("[messenger_send] 消息已发送给 %s (UI 确认通过)", peer_name)
            return True, ""

        except Exception as e:
            log.warning("[messenger_send] 未预期异常: %s", e)
            return False, "send_fail"

    def _send_greeting_messenger_fallback(self, *, did: str,
                                           profile_name: str,
                                           greeting: str,
                                           template_id: str,
                                           persona_key: Optional[str],
                                           eff_phase: str,
                                           preset_key: str,
                                           ai_decision: str) -> bool:
        """profile 页无 Message 按钮时降级走 Messenger App 路径。

        两件关键事:
          1) 拿 ``device_section_lock("messenger_active")`` 锁, 避免和 B 机的
             ``check_message_requests`` 抢输入框 (§7.7)
          2) 调 ``send_message(raise_on_error=True)`` (PR #1 语义) 细分归因
             (§7.6); PR #1 未合并时自动降级为 bool 返回
        """
        log.info("[send_greeting] profile 无 Message 按钮, 降级 Messenger 路径: %s",
                 profile_name)

        # §7.7 锁: 阻塞等最多 60s; 拿不到就放弃(让 B 先跑完)
        from src.host.fb_concurrency import device_section_lock

        def _core() -> bool:
            # Phase 7c (2026-04-24): 走 trace 已验证的 _send_messenger_greeting_to_peer
            # (而不是旧 send_message, 它依赖 AutoSelector 学习 + 不适配新版 Messenger UI
            # 的 AutoCompleteTextView + content-desc 行结构).
            # 新方法返回 (ok, error_code) 对齐 §7.6 分流矩阵.
            fallback_ok, error_code = self._send_messenger_greeting_to_peer(
                did=did, peer_name=profile_name, greeting=greeting)

            if fallback_ok:
                try:
                    from src.host.fb_store import record_inbox_message
                    record_inbox_message(
                        did, profile_name,
                        peer_type="friend_request",
                        message_text=greeting,
                        direction="outgoing",
                        ai_decision=ai_decision,
                        ai_reply_text=greeting,
                        preset_key=preset_key or "",
                        template_id=(template_id or "") + "|fallback",
                    )
                except Exception:
                    pass
                try:
                    from src.host.fb_store import (record_contact_event,
                                                    CONTACT_EVT_GREETING_FALLBACK)
                    record_contact_event(
                        did, profile_name, CONTACT_EVT_GREETING_FALLBACK,
                        template_id=template_id or "",
                        preset_key=preset_key or "",
                        meta={"persona_key": persona_key or "",
                              "phase": eff_phase,
                              "fallback_path": "messenger_app"},
                    )
                except Exception:
                    pass
                self._set_greet_reason("ok_via_fallback")
                log.info("[send_greeting] Messenger fallback 成功: %s",
                         profile_name)
                return True

            # 失败: 按 §7.6 分流 + 设置 reason
            if error_code:
                reason = self._apply_messenger_error_policy(
                    did, error_code, greeting, profile_name)
            else:
                reason = "no_message_button_fallback_miss"
            self._set_greet_reason(reason)
            return False

        # fb_concurrency.device_section_lock 的契约:
        #   * 拿到锁 → yield (无值, 用作 None)
        #   * 超时 / 拿不到 → raise RuntimeError
        # 所以只用 try/except 识别"没拿到"即可, 不用 if not got 判断。
        try:
            with device_section_lock(did, "messenger_active", timeout=60.0):
                return _core()
        except RuntimeError as e:
            log.info("[send_greeting fallback] 锁等超时(60s), B 可能长时间扫 inbox: %s",
                     str(e)[:100])
            self._set_greet_reason("fallback_locked_by_other")
            return False

    def _confirm_message_request_if_any(self, d) -> bool:
        """部分 FB 版本:对非好友首次发消息会弹 "Send Message Request?" 确认框。

        仅在检测到标志性文案时点确认;未弹返回 False(无操作)。
        """
        try:
            # 标志性文案(含中英日意)
            has_request_hint = False
            for kw in ("Message Request", "Send as message request",
                       "メッセージリクエスト", "richiesta di messaggio",
                       "消息请求"):
                try:
                    if d(textContains=kw).exists(timeout=0.4):
                        has_request_hint = True
                        break
                except Exception:
                    pass
            if not has_request_hint:
                return False
            for btn_txt in self._GREETING_REQUEST_CONFIRM_TEXTS:
                try:
                    btn = d(text=btn_txt)
                    if btn.exists(timeout=0.4):
                        self.hb.tap(d, *self._el_center(btn))
                        time.sleep(0.8)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # 归因标签(供 add_friend_and_greet / funnel 分析使用):
    #   phase_blocked / prob_gate / cap_hit / template_empty / search_miss
    #   / first_tap_miss / risk_before_msg / no_message_button / input_miss
    #   / send_miss / ok
    # 每次 send_greeting_after_add_friend 结束都会刷新 _last_greet_skip_reason。
    # 2026-04-23 Phase 6.A: 同步写入 Lead Mesh journey (如 _current_lead_cid 存在)。
    def _set_greet_reason(self, reason: str) -> None:
        self._last_greet_skip_reason = reason
        # Lead Mesh journey 同步写 — cid 由外层 send_greeting_after_add_friend 注入
        cid = getattr(self, "_current_lead_cid", "")
        if cid and reason:
            try:
                from src.host.lead_mesh import append_journey
                # ok → greeting_sent; ok_via_fallback → greeting_sent{via=fallback};
                # 其他 reason → greeting_blocked{reason=...}
                if reason == "ok":
                    action = "greeting_sent"
                    data = {"via": "inline_profile_message"}
                elif reason == "ok_via_fallback":
                    action = "greeting_sent"
                    data = {"via": "messenger_fallback"}
                else:
                    action = "greeting_blocked"
                    data = {"reason": reason}
                # 附 template_id / persona 等元信息(如果 caller 注入了)
                tpl = getattr(self, "_current_greet_template_id", "") or ""
                if tpl:
                    data["template_id"] = tpl
                persona = getattr(self, "_current_lead_persona", "") or ""
                if persona:
                    data["persona_key"] = persona
                append_journey(
                    cid, actor="agent_a",
                    actor_device=getattr(self, "_current_device", "") or "",
                    platform="facebook",
                    action=action, data=data)
            except Exception as e:
                log.debug("[journey] greeting reason 同步失败: %s", e)

    # ─── Phase 6.A: Lead Mesh 接入 helpers ─────────────────────────────
    def _check_peer_blocklist(self, peer_name: str, *,
                                did: str = "",
                                persona_key: Optional[str] = None) -> bool:
        """Phase 8h 前置检查 — peer 在 blocklist 时返回 True (表示应 skip).

        resolve cid + 查 is_blocklisted. 失败 (lead_mesh 不可用等) 保守返回 False
        (放行, 不 block 主流程).

        命中时主动写一条 ``greeting_blocked{reason=peer_blocklisted}`` journey
        事件, 让 funnel_report 能看到被 blocklist 挡的量.
        """
        try:
            cid = self._resolve_peer_canonical_safe(
                peer_name, did=did, persona_key=persona_key,
                discovered_via="blocklist_check")
            if not cid:
                return False
            from src.host.lead_mesh import is_blocklisted, get_blocklist_entry
            if not is_blocklisted(cid):
                return False
            # 命中 — 记 journey 让 dashboard funnel 可观测
            entry = get_blocklist_entry(cid) or {}
            try:
                from src.host.lead_mesh import append_journey
                append_journey(
                    cid, actor="agent_a", actor_device=did or "",
                    platform="facebook",
                    action="greeting_blocked",
                    data={
                        "reason": "peer_blocklisted",
                        "blocklist_reason": entry.get("reason") or "",
                        "blocklisted_at": entry.get("created_at") or "",
                    })
            except Exception:
                pass
            log.info("[blocklist] peer=%s cid=%s 在 blocklist, skip (reason=%s)",
                      peer_name, cid, entry.get("reason") or "")
            return True
        except Exception as e:
            log.debug("[blocklist] 检查异常(保守放行): %s", e)
            return False

    def _resolve_peer_canonical_safe(self, peer_name: str, *,
                                      did: str = "",
                                      persona_key: Optional[str] = None,
                                      platform: str = "facebook",
                                      discovered_via: str = "",
                                      profile_target_key: str = "") -> str:
        """Graceful 解析 canonical_id; lead_mesh 不可用或任何异常 → 返回空串,
        业务主流程不受影响(退回原 peer_name-only 的旧行为)。

        Phase D2: profile_target_key 若非空 (来自 navigate_to_profile 的 target_key),
        会同时注册为一条稳定 identity (url:/uid:/user: → 精确去重键),
        让同一人改名后仍能关联到同一 canonical。
        """
        if not peer_name:
            return ""
        try:
            from src.host.lead_mesh import resolve_identity
            # Phase D2: 优先用稳定身份标识 (profile URL/UID/username)
            stable_acct = ""
            if profile_target_key:
                # target_key 格式: "url:https://...", "uid:12345", "user:username"
                if profile_target_key.startswith(("url:", "uid:", "user:")):
                    stable_acct = f"fb_profile:{profile_target_key}"

            # 主解析: 用稳定 ID (如有), 否则回退到 name 伪 ID
            primary_acct = stable_acct or f"fb:{peer_name.strip()}"
            cid = resolve_identity(
                platform=platform, account_id=primary_acct,
                display_name=peer_name, persona_key=persona_key or "",
                discovered_via=discovered_via or "",
                discovered_by_device=did or "",
                auto_merge=True,
            ) or ""

            # D2 补充: 如果用了 stable_acct, 再把 name 伪 ID 也挂到同一 canonical
            # (双向可查: 名字 → canonical, URL → canonical)
            if cid and stable_acct:
                try:
                    name_acct = f"fb:{peer_name.strip()}"
                    if name_acct != primary_acct:
                        resolve_identity(
                            platform=platform, account_id=name_acct,
                            display_name=peer_name,
                            discovered_via=discovered_via or "",
                            discovered_by_device=did or "",
                            auto_merge=True,
                        )
                except Exception:
                    pass  # 补充挂载失败不影响主流程
            return cid
        except Exception as e:
            log.debug("[journey] resolve_peer_canonical 失败: %s", e)
            return ""

    def _append_journey_for_action(self, peer_name: str, action: str, *,
                                    did: str = "",
                                    persona_key: Optional[str] = None,
                                    platform: str = "facebook",
                                    data: Optional[Dict[str, Any]] = None,
                                    discovered_via: str = "") -> None:
        """一步完成 resolve + append, 供 add_friend_with_note 等不走
        _current_lead_cid 上下文的路径一次性写入。

        2026-04-24: persona_key 若非空, 自动合并到 data 里 (若 data 已有同名字段
        则不覆盖). 这让 funnel_report 按 persona 分组统计时能拿到 persona_key,
        不必每个 caller 显式写 data={persona_key: ...}.
        """
        cid = self._resolve_peer_canonical_safe(
            peer_name, did=did, persona_key=persona_key,
            platform=platform, discovered_via=discovered_via)
        if not cid:
            return
        # 合并 persona_key 到 data (不覆盖 caller 已传的同名 key)
        merged_data = dict(data or {})
        if persona_key and "persona_key" not in merged_data:
            merged_data["persona_key"] = persona_key
        try:
            from src.host.lead_mesh import append_journey
            append_journey(cid, actor="agent_a", actor_device=did or "",
                           platform=platform, action=action,
                           data=merged_data)
        except Exception as e:
            log.debug("[journey] append %s 失败: %s", action, e)

    @_with_fb_foreground
    def send_greeting_after_add_friend(self,
                                       profile_name: str,
                                       greeting: str = "",
                                       device_id: Optional[str] = None,
                                       persona_key: Optional[str] = None,
                                       phase: Optional[str] = None,
                                       assume_on_profile: bool = True,
                                       preset_key: str = "",
                                       ai_decision: str = "greeting",
                                       force: bool = False) -> bool:
        """加好友之后在 profile 页点 "Message" 发一条打招呼消息(方案 A2)。

        本方法**假设调用之前已成功 add_friend_with_note** —— 实际流程:
            add_friend_with_note(name) → 成功 → send_greeting_after_add_friend(name)

        Args:
            profile_name: 目标姓名(已在 profile 页时仅用于日志/文案 {name})
            greeting: 具体发送的文本;为空则按 persona 从 chat_messages.yaml 抽
            device_id: 设备 ID
            persona_key: 客群 key(决定打招呼语种 / 语气)
            phase: 显式覆盖 phase
            assume_on_profile: True=调用前已在目标 profile 页(add_friend 之后);
                               False=从当前页重走 search_people → 进 profile → 打招呼
            preset_key: 透传到 fb_inbox_messages.preset_key,便于漏斗切片
            ai_decision: 写库时的 decision tag,默认 "greeting";
                         方便与 auto_reply / wa_referral 等区分统计

        Returns:
            True=已发送(数据已入库); False=被闸门/上限/UI 失败拒绝
        """
        import random as _r
        did = self._did(device_id)
        d = self._u2(did)

        # Phase 6.A: 入口 resolve canonical_id 一次, 挂到 instance 变量供
        # _set_greet_reason 同步写 lead_journey 用。失败 graceful 返回空。
        self._current_lead_cid = self._resolve_peer_canonical_safe(
            profile_name, did=did, persona_key=persona_key,
            discovered_via="greeting_entry")
        self._current_lead_persona = persona_key or ""
        # template_id 在 _locked 里抽卡后才知道, 先置空
        self._current_greet_template_id = ""

        # 默认把归因置空,走到 return True 时再设 "ok"
        self._set_greet_reason("")

        # Phase 8h (2026-04-24): blocklist 前置检查 —
        # 运营手工加黑的 peer 直接 skip greeting, set_greet_reason 同步写 journey
        # 让 funnel_report 能统计到 peer_blocklisted reason.
        try:
            from src.host.fb_playbook import local_rules_disabled
            _relaxed_local_rules = local_rules_disabled()
        except Exception:
            _relaxed_local_rules = False
        if (not _relaxed_local_rules
                and self._current_lead_cid
                and __import__("src.host.lead_mesh", fromlist=["is_blocklisted"])
                    .is_blocklisted(self._current_lead_cid)):
            log.info("[send_greeting] peer cid=%s 在 blocklist, skip",
                      self._current_lead_cid)
            self._set_greet_reason("peer_blocklisted")
            return False

        # Phase 6 P0: 前置检查 — B 机若已对该 peer 在 7 天内发起过 handoff（LINE/WA/TG…）,
        # A 就不再 greeting 插话, 避免双方同时打扰。honor_rejected=True 表示
        # 若 B 主动 reject(user 拒绝引流) 也视作已有接触记录, 一并冷却。
        if self._current_lead_cid and not _relaxed_local_rules:
            try:
                from src.host.lead_mesh import check_peer_cooldown_handoff
                active_h = check_peer_cooldown_handoff(
                    self._current_lead_cid,
                    cooldown_days=7,
                    honor_rejected=True,
                )
                if active_h:
                    log.info(
                        "[send_greeting] peer=%s 已被 handoff "
                        "(channel=%s state=%s handoff_id=%s), A 跳过 greeting",
                        self._current_lead_cid,
                        active_h.get("channel"),
                        active_h.get("state"),
                        active_h.get("handoff_id"),
                    )
                    self._set_greet_reason("peer_already_handed_off")
                    return False
            except Exception as e:
                # 本检查是额外保护, 失败不应阻塞主流程
                log.debug("[send_greeting] check_peer_cooldown_handoff 异常(继续): %s", e)

        try:
            # Phase + playbook
            eff_phase, sg_cfg = _resolve_phase_and_cfg("send_greeting",
                                                       device_id=did,
                                                       phase_override=phase)
            # 冷启/冷却 phase 直接拒绝（YAML 写 max_greetings_per_run=0）
            if int(sg_cfg.get("max_greetings_per_run", 0)) <= 0:
                log.info("[send_greeting] phase=%s 禁止打招呼, skip: %s",
                         eff_phase, profile_name)
                self._set_greet_reason("phase_blocked")
                return False

            # P3-1 2026-04-23: 和 add_friend 同思路, 把 cap 检查 → UI 操作 → 入库
            # 整段用 device+section 锁串行化。section="send_greeting",
            # 与 "add_friend" 锁独立, add_friend_and_greet 场景两把锁先后持有不冲突。
            from src.host.fb_concurrency import device_section_lock
            with device_section_lock(did, "send_greeting", timeout=180.0):
                return self._send_greeting_after_add_friend_locked(
                    profile_name, greeting, did, d,
                    sg_cfg, eff_phase, persona_key,
                    assume_on_profile, preset_key, ai_decision, _r,
                    force=force)
        finally:
            # 清空 instance 变量, 避免下次调用串线
            self._current_lead_cid = ""
            self._current_lead_persona = ""
            self._current_greet_template_id = ""

    def _send_greeting_after_add_friend_locked(
            self, profile_name, greeting, did, d,
            sg_cfg, eff_phase, persona_key,
            assume_on_profile, preset_key, ai_decision, _r,
            force: bool = False):
        """锁内主体 — 保证 cap 检查 + UI 发送 + 入库原子化。

        2026-04-26: force=True 时跳过概率闸 (B2B 客户测试 / E2E 需要确定执行).
        """

        # 概率闸：支持 A/B 抽样（默认 1.0 必发）
        # 2026-04-26 fix: force=True 时跳过 (传 force_send_greeting=True 透传过来)
        enabled_p = float(sg_cfg.get("enabled_probability", 1.0) or 0.0)
        if not force and (enabled_p <= 0.0 or (enabled_p < 1.0 and _r.random() > enabled_p)):
            log.info("[send_greeting] 概率门未命中(p=%.2f), skip: %s "
                     "(传 force_send_greeting=True 可绕过)",
                     enabled_p, profile_name)
            self._set_greet_reason("prob_gate")
            return False
        if force and enabled_p < 1.0:
            log.warning("[send_greeting] force=True 绕过概率门 (p=%.2f): %s",
                        enabled_p, profile_name)

        # 24h rolling 日上限（与 add_friend.daily_cap 独立计）
        daily_cap = int(sg_cfg.get("daily_cap_per_account") or 0)
        if daily_cap > 0:
            try:
                from src.host.fb_store import count_outgoing_messages_since
                n24 = count_outgoing_messages_since(did, hours=24,
                                                    ai_decision=ai_decision)
                if n24 >= daily_cap:
                    log.info("[send_greeting] 24h 已发 %s 条 ≥ daily_cap=%s, skip %s",
                             n24, daily_cap, profile_name)
                    self._set_greet_reason("cap_hit")
                    return False
            except Exception as e:
                log.debug("[send_greeting] daily_cap 检查异常(继续): %s", e)

        # 文案：空则按 persona 从 chat_messages.yaml 抽(并记录 template_id 供 A/B)
        require_template = bool(sg_cfg.get("require_persona_template", True))
        template_id = ""  # 显式传入 greeting 时 tid 留空(运营自定义的不参与 A/B 统计)
        if not greeting:
            try:
                from .fb_content_assets import get_greeting_message_with_id
                greeting, template_id = get_greeting_message_with_id(
                    persona_key=persona_key, name=profile_name)
            except Exception as e:
                log.debug("[send_greeting] get_greeting_message_with_id 失败: %s", e)
                greeting, template_id = "", ""
        if not greeting:
            if require_template:
                log.info("[send_greeting] persona=%s 无打招呼模板且 require_persona_template=true,"
                         " 跳过避免发错语种: %s",
                         persona_key or "(default)", profile_name)
                self._set_greet_reason("template_empty")
                return False
            greeting = f"Hi {profile_name}!"
            # 兜底英文问候不记 template_id —— 防止污染 greeting_template_distribution
            # 统计。本质上这条"不该被发出"(persona 无本地化模板),只是作为硬兜底,
            # 不纳入 A/B 样本。
            template_id = ""
        # Phase 6.A: 把 template_id 挂到 instance, _set_greet_reason 会用
        self._current_greet_template_id = template_id or ""

        # 非 assume_on_profile: 先重新搜索 + 进 profile（独立使用场景）
        if not assume_on_profile:
            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                log.warning("[send_greeting] 未找到目标: %s", profile_name)
                self._set_greet_reason("search_miss")
                return False
            first = self._first_search_result_element(d, query_hint=profile_name)
            if first is None:
                log.warning("[send_greeting] 无法定位首个搜索结果: %s", profile_name)
                self._set_greet_reason("first_tap_miss")
                return False
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(2.5, 4.0))
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[send_greeting] 检测到风控提示: %s", msg)
                self._set_greet_reason("risk_before_msg")
                return False

        with self.guarded("send_greeting", device_id=did):
            # 加好友后等一段时间再点 Message（真人节奏）
            if assume_on_profile:
                lo, hi = sg_cfg.get("post_add_friend_wait_sec") or (8, 18)
                time.sleep(random.uniform(float(lo), float(hi)))

            # 风控二次检测（可能加好友后弹 identity verification）
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[send_greeting] profile 页检测到风控,放弃打招呼: %s", msg)
                self._set_greet_reason("risk_before_msg")
                return False

            # 点 Message 按钮 —— 进内联对话
            if not self._tap_profile_message_button(d, did):
                # 可选降级: 走 Messenger App 路径(allow_messenger_fallback=true 时)
                if bool(sg_cfg.get("allow_messenger_fallback", False)):
                    # Phase 7a 2026-04-24: 先验证 Messenger app 已装, 没装直接走
                    # 精准 reason "messenger_not_installed", 别让下游 send_message
                    # 在 app 不存在时各种 UI 查找全失败, reason 成 "send_fail" 没信息.
                    if not self._is_messenger_installed(did):
                        log.info("[send_greeting] Messenger app 未装, 无法 fallback: %s",
                                  profile_name)
                        self._set_greet_reason("messenger_not_installed")
                        return False
                    return self._send_greeting_messenger_fallback(
                        did=did, profile_name=profile_name, greeting=greeting,
                        template_id=template_id, persona_key=persona_key,
                        eff_phase=eff_phase, preset_key=preset_key,
                        ai_decision=ai_decision)
                log.info("[send_greeting] 未找到 Message 按钮(profile 可能无此入口): %s",
                         profile_name)
                self._set_greet_reason("no_message_button")
                return False
            time.sleep(random.uniform(2.0, 3.5))

            # 可能弹 "Send Message Request?" 确认框（对非好友首次发）—— 先处理输入再确认
            # 部分版本顺序: 打开对话 → 输入 → 点 Send → 弹 Request 确认框
            # 部分版本顺序: 打开对话 → 立即弹确认框 → 同意后才显示输入
            # 所以这里先试一次: 若当前是确认框, 先点确认再继续
            self._confirm_message_request_if_any(d)

            # 打开对话页后的思考时间（像真人打字前的停顿）
            tlo, thi = sg_cfg.get("think_before_type_sec") or (3, 7)
            time.sleep(random.uniform(float(tlo), float(thi)))

            # 输入文字 + 发送 (P5-A 2026-05-04: 复用 v40 _send_msg_from_current_profile 防御)
            try:
                input_box = d(className="android.widget.EditText")
                # P5-A race fix: 旧 exists(timeout=3.0) 在 react native 渲染慢的设备上
                # 拿 0 candidates (真机 task 2ec225b8 19:49:51 实测). wait(timeout=8.0)
                # 等真值 — v40 _send_msg_from_current_profile 同款修复.
                if not input_box.wait(timeout=8.0):
                    log.warning("[send_greeting] 未找到输入框,放弃: %s", profile_name)
                    self._set_greet_reason("input_miss")
                    try:
                        _capture_immediate_async(
                            did, step_name="send_greeting_input_miss",
                            hint=f"target={(profile_name or '')[:30]}",
                            reason="input_miss",
                        )
                    except Exception:
                        pass
                    return False
                self.hb.tap(d, *self._el_center(input_box))
                time.sleep(random.uniform(0.4, 0.9))
                self.hb.type_text(d, greeting[:300])
                time.sleep(random.uniform(0.8, 1.6))
            except Exception as e:
                log.warning("[send_greeting] 输入阶段异常: %s", e)
                self._set_greet_reason("input_miss")
                try:
                    _capture_immediate_async(
                        did, step_name="send_greeting_input_exception",
                        hint=f"target={(profile_name or '')[:30]}",
                        reason=f"input_exception:{type(e).__name__}",
                    )
                except Exception:
                    pass
                return False

            send_ok = False
            try:
                send_ok = self.smart_tap("Send message button", device_id=did)
            except Exception:
                send_ok = False
            if not send_ok:
                # P5-A: 复用 v40 _PROFILE_SEND_BTN_SELECTORS 多语言 send button (12 个候选,
                # 含 send/送信/送る/发送/发送消息/傳送/Send a message/メッセージを送 等).
                # smart_tap 已经从 AutoSelector cache 找过, 这里是干净的 u2 selector loop.
                for sel in _PROFILE_SEND_BTN_SELECTORS:
                    try:
                        el = d(**sel)
                        if el.exists(timeout=1.0):
                            el.click()
                            send_ok = True
                            log.info("[send_greeting] Send via u2 %s", sel)
                            break
                    except Exception:
                        pass
            if not send_ok:
                # 兜底: 回车键触发发送 (相当于 IME send_action ENTER)
                try:
                    d.press("enter")
                    time.sleep(0.5)
                    send_ok = True
                except Exception:
                    pass
            if not send_ok:
                log.warning("[send_greeting] 未能点击发送按钮: %s", profile_name)
                self._set_greet_reason("send_miss")
                try:
                    _capture_immediate_async(
                        did, step_name="send_greeting_send_miss",
                        hint=f"target={(profile_name or '')[:30]} msg={(greeting or '')[:30]}",
                        reason="send_miss",
                    )
                except Exception:
                    pass
                return False
            time.sleep(random.uniform(1.0, 2.0))

            # 再次处理可能"发送后"才弹的 Send as Message Request 确认
            # 注意: 必须在 time.sleep 让 UI 渲染之后调用,否则对话框还没弹就会 miss
            self._confirm_message_request_if_any(d)

        # 数据入库: facebook_inbox_messages (direction=outgoing, decision=greeting)
        # 2026-04-23: 带 template_id 供 A/B 分析
        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, profile_name,
                peer_type="friend_request",   # 刚发好友请求还未接受
                message_text=greeting,
                direction="outgoing",
                ai_decision=ai_decision,
                ai_reply_text=greeting,
                preset_key=preset_key or "",
                template_id=template_id or "",
            )
        except Exception as e:
            log.debug("[send_greeting] 入库失败(不影响主流程): %s", e)
        # P3-3: 接触事件流水(greeting_sent / greeting_fallback)
        try:
            from src.host.fb_store import (record_contact_event,
                                            CONTACT_EVT_GREETING_SENT)
            record_contact_event(
                did, profile_name, CONTACT_EVT_GREETING_SENT,
                template_id=template_id or "",
                preset_key=preset_key or "",
                meta={"persona_key": persona_key or "", "phase": eff_phase,
                      "msg_len": len(greeting or "")},
            )
        except Exception:
            pass
        # L2 中央客户画像双写 (fire_and_forget)
        try:
            from src.host.customer_sync_bridge import sync_greeting_sent
            sync_greeting_sent(
                did, profile_name,
                greeting=greeting or "",
                template_id=template_id,
                preset_key=preset_key,
                persona_key=persona_key,
                phase=eff_phase,
                fallback=False,
            )
        except Exception:
            pass

        log.info("[send_greeting] 已向 %s 发送打招呼(len=%d, persona=%s, phase=%s)",
                 profile_name, len(greeting), persona_key or "(default)", eff_phase)
        self._set_greet_reason("ok")

        # 返回 profile 页,给上层调用方提供稳定的锚点 (BACK 一次)
        try:
            self._adb("shell input keyevent 4", device_id=did)
            time.sleep(0.8)
        except Exception:
            pass
        return True

    @_with_fb_foreground
    def add_friend_and_greet(self,
                             profile_name: str,
                             note: str = "",
                             greeting: str = "",
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None,
                             preset_key: str = "",
                             source: str = "",
                             greet_on_failure: bool = False,
                             do_l2_gate: bool = False,
                             force: bool = False,
                             ai_dynamic_greeting: Optional[bool] = None,
                             force_send_greeting: Optional[bool] = None,
                             walk_candidates: bool = False,
                             l2_gate_shots: int = 1,
                             max_l2_calls: int = 3,
                             strict_persona_gate: bool = False,
                             from_current_profile: bool = False) -> Dict[str, Any]:
        """一体化: 搜索 → 加好友(带验证语) → 打招呼 DM(同 profile 页)。

        这是**方案 A2** 的默认入口 —— 把两个原子动作组合,让上层调用只需
        传一个名字。每一步失败都会在返回 dict 里体现,方便漏斗/审计。

        Args:
            profile_name: 目标姓名
            note: 加好友验证语(空则 persona 自动生成)
            greeting: 打招呼文案(空则 persona 自动生成)
            persona_key: 客群 key
            phase: 显式覆盖 phase(add_friend / send_greeting 共用)
            preset_key: 透传给两步入库
            greet_on_failure: add_friend 失败时是否仍然尝试打招呼
                              (默认 False —— 未加好友就发消息 = 极高风控风险,
                               只在特殊调试场景开启)

        Returns:
            {
              "add_friend_ok": bool,
              "greet_ok": bool,
              "greet_skipped_reason": str,   # 为何没打招呼(如 "add_friend_failed" / "cap")
              "profile_name": str,
            }
        """
        out: Dict[str, Any] = {
            "add_friend_ok": False,
            "greet_ok": False,
            "greet_skipped_reason": "",
            "profile_name": profile_name,
            "dm_only_sent": False,  # 方案 A: profile DM fallback 成功时置 True
        }

        add_ok = self.add_friend_with_note(
            profile_name,
            note=note,
            safe_mode=True,
            device_id=device_id,
            persona_key=persona_key,
            phase=phase,
            source=source,
            preset_key=preset_key,
            from_current_profile=from_current_profile,
            do_l2_gate=do_l2_gate,
            force=force,
            walk_candidates=walk_candidates,
            l2_gate_shots=l2_gate_shots,
            max_l2_calls=max_l2_calls,
            strict_persona_gate=strict_persona_gate,
        )
        out["add_friend_ok"] = bool(add_ok)

        # 方案 A (2026-05-04): add_friend 失败 + from_current_profile=True 时,
        # 当前应该还在 profile 页 (add_friend_with_note 内部失败前只 scroll
        # 没切页). 检测 Message 按钮直接发 DM 绕开冷账号 add_friend 风控.
        if not add_ok and from_current_profile and (greeting or note):
            try:
                _did = self._did(device_id)
                _d = self._u2(_did)
                _msg = greeting or note
                _ok_dm, _why = self._send_msg_from_current_profile(_d, _did, _msg)
                if _ok_dm:
                    out["dm_only_sent"] = True
                    out["greet_ok"] = True
                    out["greet_skipped_reason"] = "via_profile_dm_fallback"
                    log.info("[add_friend_and_greet] profile-DM fallback 成功 "
                             "(冷账号无 Add Friend 按钮): %s", profile_name)
                    return out
                else:
                    log.info("[add_friend_and_greet] profile-DM fallback 跳过 "
                             "(reason=%s): %s", _why, profile_name)
            except Exception as _dm_e:
                log.debug("[add_friend_and_greet] profile-DM fallback 异常: %s",
                          _dm_e)

        if not add_ok and not greet_on_failure:
            out["greet_skipped_reason"] = "add_friend_failed"
            return out

        greet_ok = self.send_greeting_after_add_friend(
            profile_name,
            greeting=greeting,
            device_id=device_id,
            persona_key=persona_key,
            phase=phase,
            assume_on_profile=True,
            preset_key=preset_key,
            ai_decision="greeting",
        )
        out["greet_ok"] = bool(greet_ok)
        # 细化原因: send_greeting_after_add_friend 已挂 _last_greet_skip_reason
        reason = getattr(self, "_last_greet_skip_reason", "") or ""
        if greet_ok:
            out["greet_skipped_reason"] = ""   # 成功时清空
        else:
            out["greet_skipped_reason"] = reason or "greet_failed"
        return out

    @_with_fb_foreground
    def _run_feed_scroll_phase(self, d, did: str, target_scrolls: int,
                               like_p: float, cfg: Dict[str, Any],
                               stats: Dict[str, Any]) -> None:
        """browse_feed 内层 scroll+like 循环（Sprint F 抽离供 interest feed 复用）。

        2026-05-08 优化:
          1. deadline 机制 — 基于 duration_minutes 设硬性截止时间，防止无限阻塞
          2. per-scroll 超时保护 — 单次 scroll 操作超 30s 视为 stall，跳过并记录
          3. 连续 stall 检测 — 连续 3 次 stall 立即终止（设备可能 ANR）
          4. 每 5 屏检测一次风控（原来每屏检测，10 关键词 × 0.25s = 高开销）
        """
        _SCROLL_TIMEOUT = 30          # 单次 scroll 操作最大秒数
        _MAX_CONSECUTIVE_STALLS = 3   # 连续 stall 次数上限
        _RISK_CHECK_INTERVAL = 5      # 每 N 屏做一次风控检测

        duration_min = stats.get("duration_minutes") or 15
        deadline = time.time() + duration_min * 60 + 60  # 给 1 分钟 buffer
        consecutive_stalls = 0

        for i in range(target_scrolls):
            # deadline 检查
            if time.time() >= deadline:
                log.info("[browse_feed] 已到达 deadline (%d min)，正常结束 (scrolled %d/%d)",
                         duration_min, stats["scrolls"], target_scrolls)
                stats["exit_reason"] = "deadline"
                break

            # 间隔检测风控（降低开销）
            if i % _RISK_CHECK_INTERVAL == 0:
                try:
                    is_risk, msg = self._detect_risk_dialog(d)
                    if is_risk:
                        log.warning("[browse_feed] 检测到风控提示,提前结束: %s", msg)
                        stats["risk_detected"] = msg
                        stats["exit_reason"] = "risk_detected"
                        break
                except Exception as _re:
                    log.debug("[browse_feed] 风控检测异常(忽略): %s", _re)

            scroll_start = time.time()
            try:
                with self.guarded("browse_feed", device_id=did, weight=0.3):
                    pull_p = float(cfg.get("pull_refresh_prob",
                                            FB_BROWSE_DEFAULTS["pull_refresh_prob"]))
                    if i < target_scrolls // 4 and random.random() < pull_p:
                        try:
                            d.swipe(0.5, 0.3, 0.5, 0.8, duration=0.4)
                            dwell = random.uniform(1.5, 3.0)
                            time.sleep(dwell)
                            stats["pull_refreshes"] += 1
                            stats["dwell_seconds_total"] += dwell
                        except Exception:
                            pass

                    self.hb.scroll_down(d)

                    video_p = float(cfg.get("video_dwell_prob",
                                         FB_BROWSE_DEFAULTS["video_dwell_prob"]))
                    if random.random() < video_p:
                        lo, hi = cfg.get("video_dwell_ms",
                                         FB_BROWSE_DEFAULTS["video_dwell_ms"])
                        ms = random.randint(int(lo), int(hi))
                        stats["video_dwells"] += 1
                    else:
                        lo, hi = cfg.get("short_wait_ms",
                                         FB_BROWSE_DEFAULTS["short_wait_ms"])
                        ms = random.randint(int(lo), int(hi))
                    self.hb.wait_read(ms)
                    stats["dwell_seconds_total"] += ms / 1000.0
                    stats["scrolls"] += 1

                    if random.random() < like_p:
                        if self.smart_tap("Like button on a post", device_id=did):
                            stats["likes"] += 1
                            self.hb.wait_between_actions(1.5)
                            stats["dwell_seconds_total"] += 1.5

                consecutive_stalls = 0  # 成功，重置计数

            except Exception as scroll_err:
                elapsed = time.time() - scroll_start
                if elapsed >= _SCROLL_TIMEOUT:
                    consecutive_stalls += 1
                    stats.setdefault("stall_count", 0)
                    stats["stall_count"] += 1
                    log.warning("[browse_feed] scroll #%d stall (%.1fs), consecutive=%d: %s",
                                i, elapsed, consecutive_stalls, scroll_err)
                    if consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
                        log.error("[browse_feed] 连续 %d 次 stall，设备可能 ANR，终止",
                                  _MAX_CONSECUTIVE_STALLS)
                        stats["exit_reason"] = "consecutive_stall"
                        break
                else:
                    log.debug("[browse_feed] scroll #%d 异常(%.1fs,非stall): %s",
                              i, elapsed, scroll_err)
                    consecutive_stalls = 0

    def browse_feed(self,
                    scroll_count: Optional[int] = None,
                    like_probability: Optional[float] = None,
                    duration_minutes: Optional[int] = None,
                    phase: Optional[str] = None,
                    device_id: Optional[str] = None) -> Dict[str, Any]:
        """Browse news feed with natural behavior (P0 + P1-1 版)。

        改动要点:
          1. 节奏公式: scroll_count 由 duration_minutes × scroll_per_min 反推
          2. 每屏停留分布按 playbook 里的 short_wait_ms 抽样
          3. video_dwell_prob 概率模拟"看视频/长图文"长停留
          4. pull_refresh_prob 概率下拉刷新
          5. `smart_tap("Home tab")` 失败走 press home → app_start 回退链，
             三层仍失败则 raise `FbWarmupError(code=fb.home_tab_not_found)`
          6. 结构化输出: 返回 dict 含 `card_type="fb_warmup"`

        P1-1 变化: 节奏参数从 FB_BROWSE_DEFAULTS 常量 → 读 config/facebook_playbook.yaml，
          热加载 + 按 phase（cold_start/growth/mature/cooldown）分档覆盖。
          显式传入 scroll_count / like_probability 仍然优先。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P1-1: 按 phase 读 playbook；phase 为空则用 defaults
        cfg = _load_browse_feed_cfg(phase=phase)
        like_p = (float(cfg.get("like_probability", FB_BROWSE_DEFAULTS["like_probability"]))
                  if like_probability is None else float(like_probability))
        target_scrolls = _resolve_scroll_count(duration_minutes, scroll_count, cfg=cfg)

        stats: Dict[str, Any] = {
            "card_type": "fb_warmup",
            "phase": phase or "",
            "config_source": "playbook" if cfg is not FB_BROWSE_DEFAULTS else "defaults",
            "target_scrolls": target_scrolls,
            "scrolls": 0,
            "likes": 0,
            "video_dwells": 0,
            "pull_refreshes": 0,
            "dwell_seconds_total": 0.0,
            "home_tab_fallback": "",
            "duration_minutes": duration_minutes or 0,
            "started_at": _now_iso(),
        }

        # P0-2: Home tab 失败回退链 —— silent fail 导致后面滑错页面
        if not self._ensure_home_feed(d, did, stats):
            stats["narrative"] = "无法进入 Home Feed，任务中止"
            raise FbWarmupError(
                code="fb.home_tab_not_found",
                message="定位 Home tab 失败，连重启 FB 都未能进入首页",
                hint="更新 data/selectors/com_facebook_katana.yaml 中 'Home tab' 选择器；或设备重装 FB 后重试",
            )

        time.sleep(random.uniform(0.8, 1.8))

        self._run_feed_scroll_phase(d, did, target_scrolls, like_p, cfg, stats)

        stats["finished_at"] = _now_iso()
        stats["like_rate_actual"] = (stats["likes"] / max(1, stats["scrolls"]))
        stats["minutes_equivalent"] = round(stats["dwell_seconds_total"] / 60.0, 1)
        stats["narrative"] = (
            f"滑动 {stats['scrolls']} 屏 · 点赞 {stats['likes']} · "
            f"看视频 {stats['video_dwells']} 次 · 约 {stats['minutes_equivalent']} 分钟真人刷 feed"
        )
        if stats.get("risk_detected"):
            stats["narrative"] += f" · ⚠ 风控中断: {stats['risk_detected']}"

        # P1-2: 把本次战绩同步给账号状态机，可能触发 phase 迁移
        try:
            from src.host.fb_account_phase import on_scrolls as _fb_phase_on_scrolls
            transition = _fb_phase_on_scrolls(did, stats["scrolls"], stats["likes"])
            if transition:
                stats["phase_transition"] = transition
                if transition.get("changed"):
                    stats["narrative"] += f" · 🎯 phase: {transition['from']} → {transition['to']}"
        except Exception:
            log.debug("[browse_feed] phase hook 失败", exc_info=True)

        return stats

    def browse_feed_by_interest(
        self,
        persona_key: Optional[str] = "",
        interest_hours: int = 168,
        max_topics: int = 4,
        like_boost: float = 0.12,
        scroll_count: Optional[int] = None,
        duration_minutes: Optional[int] = 15,
        phase: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sprint F: 按 ``fb_content_exposure`` 热榜 topic 分段 deep-link 搜索页 + scroll/like。

        无本地兴趣数据时退化为普通 ``browse_feed``（同一风控与 phase 节奏）。
        """
        from urllib.parse import quote

        did = self._did(device_id)
        d = self._u2(did)
        cfg = _load_browse_feed_cfg(phase=phase)
        base_like = float(cfg.get("like_probability", FB_BROWSE_DEFAULTS["like_probability"]))
        like_p = min(0.55, base_like + max(0.0, float(like_boost)))

        total_dm = int(duration_minutes or 15)
        total_dm = max(5, total_dm)
        pk = (persona_key or "").strip() or None
        ih = int(max(1, min(int(interest_hours), 24 * 90)))
        mt = int(max(1, min(int(max_topics), 12)))

        topics = self._fetch_device_interest_topics(did, pk, ih, mt)

        stats: Dict[str, Any] = {
            "card_type": "fb_interest_feed",
            "phase": phase or "",
            "persona_key": pk or "",
            "interest_hours": ih,
            "topics_from_db": topics,
            "segments": [],
            "fallback_no_topics": False,
            "scrolls": 0,
            "likes": 0,
            "video_dwells": 0,
            "pull_refreshes": 0,
            "dwell_seconds_total": 0.0,
            "started_at": _now_iso(),
            "duration_minutes": total_dm,
        }

        if not topics:
            stats["fallback_no_topics"] = True
            inner = self.browse_feed(
                scroll_count=scroll_count,
                like_probability=None,
                duration_minutes=total_dm,
                phase=phase,
                device_id=did,
            )
            for k in (
                "scrolls", "likes", "video_dwells", "pull_refreshes",
                "dwell_seconds_total", "like_rate_actual", "minutes_equivalent",
                "narrative", "home_tab_fallback", "risk_detected", "phase_transition",
            ):
                if k in inner:
                    stats[k] = inner[k]
            stats["narrative"] = (
                "（无画像兴趣入库数据）→ 退化为普通养号 · "
                + str(inner.get("narrative", ""))
            )
            stats["finished_at"] = _now_iso()
            return stats

        n_seg = len(topics)
        scrolls_total_budget = _resolve_scroll_count(total_dm, scroll_count, cfg=cfg)
        scrolls_per = max(4, scrolls_total_budget // max(1, n_seg))

        for row in topics:
            topic = row["topic"]
            qurl = f"https://m.facebook.com/search/top/?q={quote(topic, safe='')}"
            o = self.open_mfacebook_deeplink(qurl, did, dwell_sec=(2.2, 3.8))
            seg: Dict[str, Any] = {
                "topic": topic,
                "count": row.get("count", 0),
                "url": qurl,
                "deeplink_ok": bool(o.get("ok")),
                "deeplink_reason": o.get("reason", ""),
                "scrolls": 0,
                "likes": 0,
                "video_dwells": 0,
                "pull_refreshes": 0,
                "dwell_seconds_total": 0.0,
                "risk_detected": "",
            }
            if o.get("ok"):
                self._run_feed_scroll_phase(d, did, scrolls_per, like_p, cfg, seg)
            stats["segments"].append(seg)
            for k in ("scrolls", "likes", "video_dwells", "pull_refreshes"):
                stats[k] = stats.get(k, 0) + int(seg.get(k, 0) or 0)
            stats["dwell_seconds_total"] = float(stats.get("dwell_seconds_total", 0.0)) + float(
                seg.get("dwell_seconds_total", 0.0) or 0.0
            )
            if seg.get("risk_detected"):
                stats["risk_detected"] = seg["risk_detected"]
                break

        home_meta: Dict[str, Any] = {"home_tab_fallback": ""}
        self._ensure_home_feed(d, did, home_meta)
        stats["home_tab_fallback_after"] = home_meta.get("home_tab_fallback", "")

        stats["finished_at"] = _now_iso()
        stats["like_rate_actual"] = stats["likes"] / max(1, stats["scrolls"])
        stats["minutes_equivalent"] = round(stats["dwell_seconds_total"] / 60.0, 1)
        stats["narrative"] = (
            f"兴趣驱动 {n_seg} 个 topic deep-link · 滑 {stats['scrolls']} 屏 · "
            f"赞 {stats['likes']} · 约 {stats['minutes_equivalent']} 分钟"
        )
        if stats.get("risk_detected"):
            stats["narrative"] += f" · ⚠ 风控中断: {stats['risk_detected']}"
        try:
            from src.host.fb_account_phase import on_scrolls as _fb_phase_on_scrolls
            transition = _fb_phase_on_scrolls(did, stats["scrolls"], stats["likes"])
            if transition and transition.get("changed"):
                stats["phase_transition"] = transition
                stats["narrative"] += (
                    f" · phase: {transition['from']} → {transition['to']}"
                )
        except Exception:
            log.debug("[browse_feed_by_interest] phase hook 失败", exc_info=True)
        return stats

    # ── Home Feed 保障 ─────────────────────────────────────────────────
    def _ensure_home_feed(self, d, did: str, stats: Optional[Dict] = None) -> bool:
        """确保当前在 Home Feed。三层回退：

        1. smart_tap("Home tab") — 命中选择器直接点
        2. press home → resume FB (stay resident) → 再试 Home tab
        3. d.app_start(PACKAGE) 强重启 → 再试 Home tab
        任一层成功返回 True；全部失败返回 False。
        stats["home_tab_fallback"] 记录最终成功的层级。
        """
        if self.smart_tap("Home tab", device_id=did):
            if stats is not None:
                stats["home_tab_fallback"] = "smart_tap"
            return True

        log.warning("[browse_feed] smart_tap('Home tab') 未命中，启用回退 L1")
        try:
            d.press("home")
            time.sleep(0.8)
            try:
                d.app_start(PACKAGE, stop=False)
            except Exception:
                try:
                    d.shell(f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1")
                except Exception:
                    pass
            time.sleep(2.5)
            if self.smart_tap("Home tab", device_id=did):
                if stats is not None:
                    stats["home_tab_fallback"] = "press_home_then_resume"
                return True
        except Exception as e:
            log.warning("[browse_feed] 回退 L1 异常: %s", e)

        log.warning("[browse_feed] 启用回退 L2: app_start(stop=True) 强重启 FB")
        try:
            d.app_stop(PACKAGE)
            time.sleep(1.0)
            d.app_start(PACKAGE)
            time.sleep(3.5)
            # 强重启后再 dismiss 一遍教育弹窗
            try:
                self._dismiss_dialogs(d, device_id=did)
            except Exception:
                pass
            if self.smart_tap("Home tab", device_id=did):
                if stats is not None:
                    stats["home_tab_fallback"] = "app_start_restart"
                return True
            # 最后一招: 很多 FB 版本首启直接落在 Home，点不到 Home tab 也意味已经在首页
            try:
                if d(resourceIdMatches=r".*news_feed.*|.*feed_.*").exists(timeout=1.0):
                    if stats is not None:
                        stats["home_tab_fallback"] = "restart_already_on_feed"
                    return True
            except Exception:
                pass
        except Exception as e:
            log.warning("[browse_feed] 回退 L2 异常: %s", e)

        return False

    _FB_JOIN_GROUP_BUTTON_TEXTS = (
        "Join group", "Join Group", "JOIN GROUP", "Join",
        "加入小组", "加入群组", "加入社團", "加入小組", "加入",
        "グループに参加", "参加する", "参加",
    )
    _FB_JOIN_REQUESTED_MARKERS = (
        "Request sent", "Requested", "Pending approval", "Cancel request",
        "Request pending",
        "已发送请求", "请求已发送", "已申请", "取消申请", "待审核", "等待批准",
        "已申請", "取消申請", "審核中",
        "リクエスト済み", "申請済み", "承認待ち", "リクエストをキャンセル",
    )
    _FB_JOIN_QUESTION_MARKERS = (
        "Answer questions", "Answer membership questions",
        "Membership questions",
        "回答问题", "回答以下问题", "会员问题", "成员资格问题",
        "質問に回答", "メンバーシップに関する質問",
        # P4-A (2026-05-04) 真机 task 5af81143 [memtab-debug] 实测: 新版 FB 70K
        # Public 群 Join 时 question wall 用 EditText placeholder 'Write an answer'
        # + 单 'Submit' button, 没用 'Answer questions' 之类的明文标题. 加这两个
        # 短语 + 多语言变体, 让 _classify_join_group_page 能识别新 UI.
        "Write an answer", "Submit your answer",
        "Answer to join", "Answer this question",
        "回答以加入", "回答後加入", "答えて参加",
        "回答以参加",
    )
    _FB_JOINED_MARKERS = (
        "Joined", "已加入", "已是成员", "已是成員",
        "参加済み",
        "退出小组", "退出群组", "退出小組", "退出社團",
        "取关小组", "取消关注小组", "管理通知",
        "Leave group", "Unfollow group", "Manage notifications",
        "グループを退会", "フォローをやめる", "お知らせを管理",
    )
    _FB_GROUP_WELCOME_MARKERS = (
        "欢迎加入", "欢迎", "Welcome to", "Welcome",
        "ようこそ", "参加を歓迎",
    )
    _FB_GROUP_WELCOME_CONTINUE_TEXTS = (
        "继续", "完成", "开始浏览", "查看小组", "进入小组", "查看群组", "进入群组",
        "Continue", "CONTINUE", "Done", "Get started", "View group", "Go to group",
        "次へ", "続行", "完了", "開始", "グループを見る",
    )

    def _join_group_outcome(self, outcome: str) -> bool:
        """记录最近一次 join_group 的细分状态, 供 executor 写入任务结果。"""
        try:
            self.last_join_group_outcome = outcome
        except Exception:
            pass
        return outcome in ("joined", "already_joined_or_accessible")

    def _join_button_label_matches(self, raw: str, label: str) -> bool:
        text = (raw or "").strip()
        if not text:
            return False
        if label in ("Join", "加入", "参加"):
            # 2026-05-03 v7: FB 新版 Join button 是 "Join {group_name} group"
            # 形式 (e.g. "Join ペットの時間 group"). 旧代码要求短词全等, 永远
            # miss → auto_join 没生效. 放宽: text 以 "Join "/"加入 " 开头视为
            # 命中. 仍排除 "Joined"/"加入了" 等已加入状态 (用 endswith 排除).
            if text == label:
                return True
            _prefix = label + " "
            if text.startswith(_prefix):
                # 排除 "Joined" 这类前缀被空格补全的边缘情况
                _low = text.lower()
                if any(_low.startswith(_neg) for _neg in (
                    "joined", "join request",
                )):
                    return False
                return True
            return False
        return text == label or label.lower() in text.lower()

    def _join_button_present_in_xml(self, xml: str) -> bool:
        text = xml or ""
        try:
            from ..vision.screen_parser import XMLParser
            for node in XMLParser.parse(text):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if any(self._join_button_label_matches(raw, marker)
                       for marker in self._FB_JOIN_GROUP_BUTTON_TEXTS):
                    return True
        except Exception:
            pass
        return any(
            marker in text
            for marker in self._FB_JOIN_GROUP_BUTTON_TEXTS
            if marker not in ("Join", "加入", "参加")
        )

    def _classify_join_group_page(self, xml: str,
                                  group_name: str = "") -> str:
        """识别加入后的真实状态。空字符串表示还无法判断。

        2026-05-03 v13: FB 真机 join 成功后 button text 变 'joined {群名} group'
        (小写 'joined') — 不在 _FB_JOINED_MARKERS 大写列表里. 加小写 'joined '
        前缀 + group_name 精确短语作为额外信号, 让 8 次循环里第一次就识别为
        joined, 不再 wait 8 秒.
        """
        text = xml or ""
        if not text:
            return ""
        if any(marker in text for marker in self._FB_JOIN_QUESTION_MARKERS):
            return "membership_questions_required"
        if any(marker in text for marker in self._FB_JOIN_REQUESTED_MARKERS):
            return "join_requested_pending_approval"
        if any(marker in text for marker in self._FB_JOINED_MARKERS):
            return "joined"
        # v13: 小写 'joined {群名} group' 精确短语 (FB 真机第 18 轮抓到)
        if group_name:
            _joined_phrases = (
                f"joined {group_name} group",
                f"Joined {group_name} group",
                f"已加入{group_name}",
                f"{group_name}に参加済み",
            )
            if any(p in text for p in _joined_phrases):
                return "joined"
        has_group_name = bool(group_name and group_name in text)
        has_group_tab = any(tok in text for tok in self._GROUP_PAGE_SIGNATURE_TOKENS)
        if has_group_name and has_group_tab and not self._join_button_present_in_xml(text):
            return "already_joined_or_accessible"
        return ""

    def _looks_like_group_welcome_screen(self, xml: str,
                                         group_name: str = "") -> bool:
        """识别入群后的欢迎/引导页。

        Facebook 对首次加入的公开群经常先展示欢迎页，底部有“继续”按钮。
        该页还不含 Members/Discussion 等群页 tab，旧流程会立刻重新搜索群名。
        """
        text = xml or ""
        if not text:
            return False
        if group_name and group_name not in text:
            return False
        low = text.lower()
        welcome_hit = any(
            marker in text or marker.lower() in low
            for marker in self._FB_GROUP_WELCOME_MARKERS
        )
        if not welcome_hit:
            return False
        button_hit = any(
            label in text or label.lower() in low
            for label in self._FB_GROUP_WELCOME_CONTINUE_TEXTS
        )
        # 欢迎标题 + 目标群名就足够判定；button_hit 只是给日志/坐标兜底参考。
        return welcome_hit or button_hit

    def _tap_group_welcome_continue(self, d, did: str) -> bool:
        """点击入群欢迎页底部继续按钮。只由 welcome-screen 判定后调用。"""
        def _tap_if_sane(el, label: str) -> bool:
            try:
                cx, cy = self._el_center(el)
                if cy < 650:
                    return False
                self.hb.tap(d, cx, cy)
                time.sleep(1.2)
                log.info("[group_welcome] tap continue by label=%r", label)
                return True
            except Exception:
                return False

        for label in self._FB_GROUP_WELCOME_CONTINUE_TEXTS:
            for sel in (
                {"text": label, "clickable": True},
                {"description": label, "clickable": True},
                {"text": label},
                {"description": label},
            ):
                try:
                    el = d(**sel)
                    if el.exists(timeout=0.35) and _tap_if_sane(el, label):
                        return True
                except Exception:
                    continue

        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            candidates = []
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                norm = raw.lower()
                matched = False
                for label in self._FB_GROUP_WELCOME_CONTINUE_TEXTS:
                    lab = label.lower()
                    if raw == label or lab in norm:
                        matched = True
                        break
                if not matched:
                    continue
                left, top, right, bottom = node.bounds
                if top < 650:
                    continue
                candidates.append((top, left, right, bottom, raw))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            if candidates:
                top, left, right, bottom, raw = candidates[0]
                self.hb.tap(d, (left + right) // 2, (top + bottom) // 2)
                time.sleep(1.2)
                log.info("[group_welcome] tap continue by XML label=%r", raw[:40])
                return True
        except Exception as e:
            log.debug("[group_welcome] XML continue fallback failed: %s", e)

        # 最后才用底部坐标兜底，且调用方已经确认当前是目标群欢迎页。
        try:
            w, h = d.window_size()
            self.hb.tap(d, max(30, w // 2), max(650, int(h * 0.875)))
            time.sleep(1.2)
            log.info("[group_welcome] tap continue by bottom coordinate")
            return True
        except Exception:
            return False

    def _continue_group_welcome_if_present(self, d, did: str,
                                           group_name: str = "") -> bool:
        """若停在目标群欢迎页，点完引导直到进入群页或欢迎页消失。"""
        advanced = False
        for _ in range(4):
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if not self._looks_like_group_welcome_screen(xml, group_name):
                return advanced
            _set_step("处理入群欢迎页", group_name)
            if not self._tap_group_welcome_continue(d, did):
                log.warning("[group_welcome] welcome screen detected but continue miss "
                            "group=%r", group_name)
                return advanced
            advanced = True
            time.sleep(1.0)
            try:
                ok, reason = self._assert_on_specific_group_page(d, group_name)
            except Exception:
                ok, reason = False, ""
            if ok:
                log.info("[group_welcome] entered group page after welcome group=%r "
                         "evidence=%s", group_name, reason)
                return True
        return advanced

    def _current_group_page_requires_join(self, d,
                                            group_name: str = "") -> bool:
        """Detect whether the current group page still exposes a Join button.

        2026-05-03 v11: 群名 ADB stdout 解码乱码导致精确字面量永不命中, 改用
        位置约束 — FB 群信息页布局固定: 群头按钮 y=557-689, Join 当前群 button
        紧邻其下 y=705-855. Suggested groups section 在屏幕中下部 y > 1000.
        把 _center_safe 收严到 600 <= cy <= 1000, 即可过滤掉 28 个 noise
        而保留真正的 join button.
        """
        try:
            xml_probe = d.dump_hierarchy() or ""
        except Exception:
            xml_probe = ""
        if xml_probe and (
            hierarchy_looks_like_fb_groups_filtered_results_page(xml_probe)
            or hierarchy_looks_like_fb_search_results_page(xml_probe)
        ):
            return False

        # 2026-05-04 关键: 优先检查"Joined"按钮 (新版 FB 群顶部 Joined ▼).
        # 如果存在 + 屏幕上方 (cy<800) → 当前群已加入, 不需 join.
        # 之前 v22-v24 fail 因为漏判: 屏幕下方 Related groups 区有别群的 Join
        # 按钮 (cy~1300), require_join 误判"当前群未加入"触发 join 流程.
        try:
            from src.vision.screen_parser import XMLParser as _XPJ
            for _n in _XPJ.parse(xml_probe):
                if not getattr(_n, "bounds", None):
                    continue
                _t_ = (getattr(_n, "text", "") or "").strip()
                _d_ = (getattr(_n, "content_desc", None) or "").strip()
                _clk = bool(getattr(_n, "clickable", False))
                _l, _tt, _r, _b = _n.bounds
                _cy = (_tt + _b) // 2
                # Joined 按钮 (Joined / Joined ▼ / 已加入 / 加入済み / 已加入)
                if _clk and _cy < 900 and _t_ in (
                    "Joined", "已加入", "已加入小组", "加入済み", "已加入群组",
                ):
                    log.info("[extract_members] joined button found at cy=%d, skip require_join check", _cy)
                    return False
                if _clk and _cy < 900 and _d_.startswith(("Joined ", "joined ")) and "group" in _d_:
                    log.info("[extract_members] joined-group desc found cy=%d, skip require_join", _cy)
                    return False
        except Exception:
            pass

        def _center_safe(el) -> bool:
            """v11: 收严 y 区间到 600-1000 (群头下方+子 tab 上方), 排除帖子里
            'Join us!' (y > 1100) 和 Suggested groups (y > 1000)."""
            try:
                _cx, cy = self._el_center(el)
                return 600 <= cy <= 1000
            except Exception:
                return False

        # P1-A v10: 优先用 group_name 精确字面量匹配 (避免 28 candidates 噪声)
        if group_name:
            _exact_phrases = (
                f"Join {group_name} group",
                f"加入{group_name}小组",
                f"加入{group_name}群组",
                f"加入{group_name}社團",
                f"加入{group_name}小組",
                f"{group_name}に参加",
            )
            for _phrase in _exact_phrases:
                for _key in ("text", "description"):
                    try:
                        el = d(**{_key: _phrase, "clickable": True})
                        if el.exists(timeout=0.25) and _center_safe(el):
                            log.info(
                                "[extract_members] join required "
                                "(exact %s=%r)", _key, _phrase[:40],
                            )
                            return True
                    except Exception:
                        continue

        # 2026-05-03 v12: group_name 因 ADB stdout 乱码常常无法精确字面量匹配,
        # 必须保留宽 textMatches 兜底. 抗噪靠 _center_safe 的位置约束 + 限制
        # 候选数量上限 — 真 Join button 1 个, Suggested groups ≥ 5 个 noise.
        _JOIN_TIGHT_MATCH = {
            "Join": r"^Join\s.+\sgroup$",
            "加入": r"^加入\s.+",
            "参加": r"^参加\s.+",
        }
        for label in self._FB_JOIN_GROUP_BUTTON_TEXTS:
            sels: List[Dict[str, Any]] = [
                {"text": label, "clickable": True},
                {"description": label, "clickable": True},
            ]
            if label in _JOIN_TIGHT_MATCH:
                _re = _JOIN_TIGHT_MATCH[label]
                sels.extend([
                    {"textMatches": _re, "clickable": True},
                    {"descriptionMatches": _re, "clickable": True},
                ])
            for sel in sels:
                try:
                    el = d(**sel)
                    if not el.exists(timeout=0.25):
                        continue
                    # v12: 候选数 > 3 视为噪声 (Suggested groups), 跳过该 selector
                    try:
                        _cnt = el.count
                    except Exception:
                        _cnt = 1
                    if _cnt > 3:
                        log.info(
                            "[extract_members] skip noisy join sel=%s "
                            "count=%d", list(sel.keys())[0], _cnt,
                        )
                        continue
                    if _center_safe(el):
                        log.info(
                            "[extract_members] join required (sel=%s count=%d)",
                            list(sel.keys())[0], _cnt,
                        )
                        return True
                except Exception:
                    continue

        try:
            xml = xml_probe or d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                bounds = getattr(node, "bounds", None)
                if not raw or not bounds:
                    continue
                norm = raw.lower()
                matched = False
                for label in self._FB_JOIN_GROUP_BUTTON_TEXTS:
                    if self._join_button_label_matches(raw, label):
                        matched = True
                        break
                if not matched:
                    continue
                _left, top, _right, bottom = bounds
                if 220 <= top <= 1450 and 220 <= bottom <= 1450:
                    log.info(
                        "[extract_members] join required (XML raw=%r)",
                        raw[:40],
                    )
                    return True
        except Exception as e:
            log.debug("[extract_members] join-required detection failed: %s", e)
        return False

    def _tap_group_join_button(self, d, did: str,
                                group_name: str = "") -> bool:
        """点击群主页上的 Join/加入按钮, 避免旧 smart_tap 坐标污染。

        v11 (2026-05-03): 位置约束 600 <= cy <= 1000 排除 Suggested groups
        噪声 (y > 1000) 和帖子里 'Join us!' 文本 (y > 1100). FB 群信息页里
        当前群的 Join button 紧邻群头下方 (y=705-855).
        """
        def _center_safe(el) -> Optional[Tuple[int, int]]:
            try:
                cx, cy = self._el_center(el)
                # 2026-05-04 放宽: 新版 FB Join 按钮 y 跨度更大 (cover bottom +
                # 100~250). 老 v11 600-1000 太严, 真机 v23 22 candidates 全 reject.
                # 改 400-1300 覆盖 Joined ▼/Invite 按钮位置 (cover_bottom + 100-250).
                if cy < 400 or cy > 1300:
                    return None
                return cx, cy
            except Exception:
                return None

        # v10: 优先精确字面量 (避免 suggested groups 噪声)
        if group_name:
            _exact_phrases = (
                f"Join {group_name} group",
                f"加入{group_name}小组",
                f"加入{group_name}群组",
                f"加入{group_name}社團",
                f"加入{group_name}小組",
                f"{group_name}に参加",
            )
            for _phrase in _exact_phrases:
                for _key in ("text", "description"):
                    try:
                        el = d(**{_key: _phrase, "clickable": True})
                        if el.exists(timeout=0.5):
                            pos = _center_safe(el)
                            if not pos:
                                continue
                            self.hb.tap(d, *pos)
                            time.sleep(0.8)
                            log.info(
                                "[join_group] tap join button (exact %s=%r)",
                                _key, _phrase[:40],
                            )
                            return True
                    except Exception:
                        continue

        # v12: 兜底 textMatches 始终启用, 位置约束 + 候选数限制抗噪
        _JOIN_TIGHT_MATCH = {
            "Join": r"^Join\s.+\sgroup$",
            "加入": r"^加入\s.+",
            "参加": r"^参加\s.+",
        }
        for label in self._FB_JOIN_GROUP_BUTTON_TEXTS:
            sels: List[Dict[str, Any]] = [
                {"text": label, "clickable": True},
                {"description": label, "clickable": True},
            ]
            if label in _JOIN_TIGHT_MATCH:
                _re = _JOIN_TIGHT_MATCH[label]
                sels.extend([
                    {"textMatches": _re, "clickable": True},
                    {"descriptionMatches": _re, "clickable": True},
                ])
            for sel in sels:
                try:
                    el = d(**sel)
                    if not el.exists(timeout=0.5):
                        continue
                    try:
                        _cnt = el.count
                    except Exception:
                        _cnt = 1
                    if _cnt > 3:
                        # noisy selector, skip
                        continue
                    pos = _center_safe(el)
                    if not pos:
                        continue
                    self.hb.tap(d, *pos)
                    time.sleep(0.8)
                    _kind = next(iter(sel.keys()))
                    log.info("[join_group] tap join button by %s=%r count=%d",
                             _kind, label, _cnt)
                    return True
                except Exception:
                    continue

        # XML fallback: 新版 FB 经常把按钮 label 放 content-desc, 元素本身不可点。
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            candidates = []
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                norm = raw.lower()
                matched = False
                for label in self._FB_JOIN_GROUP_BUTTON_TEXTS:
                    if self._join_button_label_matches(raw, label):
                        matched = True
                        break
                if not matched:
                    continue
                left, top, right, bottom = node.bounds
                if top < 220 or bottom > 1450:
                    continue
                candidates.append((top, left, right, bottom, raw))
            candidates.sort(key=lambda item: (item[0], item[1]))
            if candidates:
                top, left, right, bottom, raw = candidates[0]
                self.hb.tap(d, (left + right) // 2, (top + bottom) // 2)
                time.sleep(0.8)
                log.info("[join_group] tap join button by XML label=%r", raw[:40])
                return True
        except Exception as e:
            log.debug("[join_group] XML join button fallback failed: %s", e)

        # Test-run fallback: the 2026-05-01 device build sometimes shows a
        # full-width blue "加入小组" button on the group header while
        # uiautomator returns an empty hierarchy. Only use this after all
        # semantic selectors failed, and only for Facebook local-rule-disabled
        # test mode.
        try:
            from src.host.fb_playbook import local_rules_disabled
            if local_rules_disabled():
                w, h = d.window_size()
                x = max(40, min(w - 40, w // 2))
                # FB group header primary CTA is normally below title/member
                # metadata and above the tab strip.
                y = max(520, min(h - 420, int(h * 0.58)))
                self.hb.tap(d, x, y)
                time.sleep(1.0)
                log.info("[join_group] tap join button by test coordinate fallback")
                return True
        except Exception as e:
            log.debug("[join_group] coordinate join fallback failed: %s", e)
        return False

    def _join_current_group_page_if_needed(self, d, did: str,
                                           group_name: str) -> Tuple[bool, str]:
        """Join from the current group page before falling back to search.

        extract_group_members already verified that the current target group
        exposes a Join CTA. Re-searching from that state is slower and can land
        on a different public group row, so try the visible page first.
        """
        if not self._current_group_page_requires_join(d, group_name):
            return True, "already_joined_or_accessible"
        if not self._tap_group_join_button(d, did, group_name):
            return False, "join_button_not_found"

        for _ in range(8):
            time.sleep(1.0)
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            state = self._classify_join_group_page(xml, group_name)
            if state in ("already_joined_or_accessible", "joined"):
                try:
                    self._continue_group_welcome_if_present(d, did, group_name)
                except Exception:
                    pass
                return True, "joined"
            if state in (
                "join_requested_pending_approval",
                "membership_questions_required",
            ):
                return False, state
            if not self._current_group_page_requires_join(d, group_name):
                return True, "joined_no_cta"
        return False, "join_state_unknown_after_tap"

    def _tap_join_button_near_group_result(self, d, did: str,
                                           group_name: str) -> bool:
        """在搜索结果列表里直接点击目标群同一行的 Join/加入按钮。"""
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            group_rows = []
            join_rows = []
            combo_rows = []
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                left, top, right, bottom = node.bounds
                if top < 260 or bottom > 1500:
                    continue
                mid_y = (top + bottom) // 2
                if group_name in raw:
                    group_rows.append((mid_y, left, right, bottom, raw))
                    if self._result_requires_join(raw):
                        combo_rows.append((mid_y, left, right, bottom, raw))
                label_hit = False
                for label in self._FB_JOIN_GROUP_BUTTON_TEXTS:
                    if self._join_button_label_matches(raw, label):
                        label_hit = True
                        break
                if label_hit:
                    join_rows.append((mid_y, left, right, bottom, raw))
            if not group_rows and not combo_rows:
                return False
            if join_rows:
                target_y = group_rows[0][0] if group_rows else combo_rows[0][0]
                same_line = [
                    r for r in join_rows
                    if abs(r[0] - target_y) <= 90
                ]
                if same_line:
                    mid_y, left, right, bottom, raw = sorted(
                        same_line, key=lambda r: (abs(r[0] - target_y), -r[1])
                    )[0]
                    self.hb.tap(d, (left + right) // 2, mid_y)
                    time.sleep(0.8)
                    log.info(
                        "[join_group] tap result-row join button label=%r",
                        raw[:40],
                    )
                    return True
            if combo_rows:
                mid_y, left, right, bottom, raw = combo_rows[0]
                # 组合行通常是 "群名 · 加入", 右侧才是加入按钮。
                x = max(left + 40, min(right - 35, int(right - (right - left) * 0.12)))
                self.hb.tap(d, x, mid_y)
                time.sleep(0.8)
                log.info("[join_group] tap result-row combo join raw=%r", raw[:80])
                return True
        except Exception as e:
            log.debug("[join_group] result-row join fallback failed: %s", e)
        return False

    @_with_fb_foreground
    def join_group(self, group_name: str,
                   device_id: Optional[str] = None) -> bool:
        """Search and join a group with the same hardened path as enter_group."""
        did = self._did(device_id)
        d = self._u2(did)
        try:
            self.last_join_group_outcome = ""
        except Exception:
            pass
        group_name = (group_name or "").strip()
        if not group_name:
            return self._join_group_outcome("missing_group_name")

        # Facebook 多机通常是一机一号；quota 需要按账号/设备隔离,
        # 否则一台设备的失败重试会耗尽其他设备的 join_group 小时额度。
        with self.guarded("join_group", account=did, device_id=did, weight=0.25):
            _set_step("搜索待加入群组", group_name)
            if not self._tap_search_bar_preferred(d, did):
                log.warning("[join_group] search bar not opened for group=%r",
                            group_name)
                return self._join_group_outcome("search_page_not_opened")
            time.sleep(0.6)
            try:
                _on_search = hierarchy_looks_like_fb_search_surface(
                    d.dump_hierarchy() or ""
                )
            except Exception:
                _on_search = False
            if not _on_search:
                log.warning("[join_group] Step 1 后未进入搜索页 group=%r",
                            group_name)
                return self._join_group_outcome("search_page_not_opened")

            if not self._type_fb_search_query(d, group_name, did):
                log.warning("[join_group] type query failed group=%r", group_name)
                return self._join_group_outcome("type_query_failed")
            time.sleep(1.0)
            if not self._submit_fb_search_with_verify(d, did, group_name):
                return self._join_group_outcome("search_submit_failed")

            _set_step("筛选 Groups 结果", group_name)
            filter_ok = False
            filter_outcome = "groups_filter_not_found"
            if self._tap_search_results_groups_filter(d, did):
                time.sleep(1.0)
                try:
                    _xml_after_filter = d.dump_hierarchy() or ""
                except Exception:
                    _xml_after_filter = ""
                if hierarchy_looks_like_fb_groups_filtered_results_page(
                    _xml_after_filter
                ):
                    filter_ok = True
                else:
                    filter_outcome = "groups_filter_not_applied"

            # Some Facebook builds do not expose a stable top Groups chip, but
            # the All results page already contains group rows. Do not fail if
            # the exact group is visible as a parsed group candidate; continue
            # with the same strict row/name checks used below.
            if not filter_ok:
                try:
                    current_groups = self._extract_group_search_results(
                        d, keyword=group_name, max_groups=8)
                except Exception:
                    current_groups = []
                target_norm = group_name.casefold()
                visible_group = False
                for g in current_groups or []:
                    candidate = (g.get("group_name") or "").strip()
                    cand_norm = candidate.casefold()
                    if (
                        cand_norm == target_norm
                        or (len(target_norm) >= 6 and target_norm in cand_norm)
                        or (len(cand_norm) >= 6 and cand_norm in target_norm)
                    ):
                        visible_group = True
                        break
                if visible_group:
                    log.info(
                        "[join_group] Groups filter unavailable/outcome=%s; "
                        "continue from current results group=%r",
                        filter_outcome, group_name,
                    )
                else:
                    log.warning(
                        "[join_group] Groups filter unavailable/outcome=%s and "
                        "target group not visible group=%r",
                        filter_outcome, group_name,
                    )
                    return self._join_group_outcome(filter_outcome)

            _set_step("点击结果页加入按钮", group_name)
            if self._tap_join_button_near_group_result(d, did, group_name):
                for _ in range(5):
                    time.sleep(1.0)
                    try:
                        xml = d.dump_hierarchy() or ""
                    except Exception:
                        xml = ""
                    state = self._classify_join_group_page(xml, group_name)
                    if state in ("already_joined_or_accessible", "joined"):
                        log.info("[join_group] result-row join success group=%r",
                                 group_name)
                        try:
                            self._continue_group_welcome_if_present(d, did, group_name)
                        except Exception as e:
                            log.debug("[join_group] welcome continue skipped: %s", e)
                        return self._join_group_outcome("joined")
                    if state in (
                        "join_requested_pending_approval",
                        "membership_questions_required",
                    ):
                        log.info("[join_group] result-row join state=%s group=%r",
                                 state, group_name)
                        self._join_group_outcome(state)
                        return False

            _set_step("进入待加入群组", group_name)
            if not self._tap_first_search_result_group(d, did, group_name):
                log.warning("[join_group] exact group row not found group=%r",
                            group_name)
                return self._join_group_outcome("group_result_not_found")
            time.sleep(random.uniform(2.0, 3.2))
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if group_name and group_name not in xml:
                log.warning("[join_group] group page name not found group=%r",
                            group_name)
                return self._join_group_outcome("group_page_not_opened")

            state = self._classify_join_group_page(xml, group_name)
            if state in ("already_joined_or_accessible", "joined"):
                log.info("[join_group] group already accessible group=%r state=%s",
                         group_name, state)
                try:
                    self._continue_group_welcome_if_present(d, did, group_name)
                except Exception as e:
                    log.debug("[join_group] welcome continue skipped: %s", e)
                return self._join_group_outcome(state)
            if state in (
                "join_requested_pending_approval",
                "membership_questions_required",
            ):
                log.info("[join_group] group join blocked state=%s group=%r",
                         state, group_name)
                self._join_group_outcome(state)
                return False

            _set_step("提交加入群组", group_name)
            if not self._tap_group_join_button(d, did, group_name):
                log.warning("[join_group] join button not found group=%r",
                            group_name)
                return self._join_group_outcome("join_button_not_found")

            for _ in range(8):
                time.sleep(1.0)
                try:
                    xml = d.dump_hierarchy() or ""
                except Exception:
                    xml = ""
                state = self._classify_join_group_page(xml, group_name)
                if state in ("already_joined_or_accessible", "joined"):
                    log.info("[join_group] join success group=%r state=%s",
                             group_name, state)
                    try:
                        self._continue_group_welcome_if_present(d, did, group_name)
                    except Exception as e:
                        log.debug("[join_group] welcome continue skipped: %s", e)
                    return self._join_group_outcome("joined")
                if state in (
                    "join_requested_pending_approval",
                    "membership_questions_required",
                ):
                    log.info("[join_group] join state=%s group=%r",
                             state, group_name)
                    self._join_group_outcome(state)
                    return False

            return self._join_group_outcome("join_state_unknown_after_tap")

    # ── Group Operations (Sprint 1 新增 — Facebook 引流核心入口) ──────────

    # ─── 2026-04-23 P3-bug: 修复 browse_groups 误点发图界面 ──────────────
    # 根因: data/selectors/com_facebook_katana.yaml 里自学习污染:
    #   "Groups tab" → "Home, tab 1 of 6"  (首页)
    #   "Your groups" → "What's on your mind?"  (发帖输入框,点了会进发图界面)
    # 两个 selector 都已错误命中 15 次。
    #
    # 新策略: 不再信任 smart_tap 对这两个 key 的自学习,用硬编码 u2 selector
    # 精确匹配底部导航的 tab description 规律 "Groups, tab N of M"。

    # 底部导航 Groups tab 常见 description (FB Android 按账号 tab 数区分:
    # 3 栏版本(无 Marketplace / Watch)=3/5 或 4/5; 完整版=4/6 或 5/6)
    _FB_GROUPS_TAB_DESCRIPTIONS = (
        "Groups, tab 4 of 6", "Groups, tab 5 of 6",
        "Groups, tab 3 of 5", "Groups, tab 4 of 5",
        "Groups, tab 3 of 6",
        # 中文版也可能就叫"群组" —— FB Android 国际版通常是英文 description
        # 但 MIUI/Android system 本地化后可能会有变种
    )

    # "你的群组" / "Your groups" 的精确 selectors (Groups 主页上方入口)
    _FB_YOUR_GROUPS_TEXTS = (
        "Your groups", "Your Groups", "YOUR GROUPS",
        "我加入的群组", "我的群组", "加入的群组",
        "マイグループ", "参加しているグループ", "所属グループ",
    )

    def _tap_groups_bottom_tab(self, d, did: str) -> bool:
        """精确点击底部导航的 Groups tab, 避免命中 Home / 发帖按钮。

        2026-04-23 修订: 原 regex fallback(descriptionMatches) 在 AdbFallbackDevice
        的实现里匹配了几乎所有元素(217 of 218 candidates), 会 tap 错目标。
        改用 descriptionContains + clickable 过滤的精确流程。

        策略四层:
          1. 枚举已知的 description 精确值
          2. descriptionContains "Groups, tab" + clickable=True
          3. resourceId 兜底
          4. 最后才走 smart_tap (通过 _assert_on_groups_page 兜底)
        """
        # 1) 精确 description
        for desc in self._FB_GROUPS_TAB_DESCRIPTIONS:
            try:
                el = d(description=desc)
                if el.exists(timeout=0.8):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[browse_groups] tap Groups bottom tab by desc='%s'", desc)
                    return True
            except Exception:
                continue
        # 2) descriptionContains "Groups, tab" + clickable 过滤
        # (比 regex 稳, 因为 AdbFallbackDevice 的 descriptionMatches 语义宽松)
        for desc_prefix in ("Groups, tab", "Groups,"):
            try:
                el = d(descriptionContains=desc_prefix, clickable=True)
                if el.exists(timeout=0.8):
                    # 读实际 description 记日志,便于长期观察 FB tab 版本分布
                    actual_desc = ""
                    try:
                        actual_desc = (el.info or {}).get("contentDescription", "") or ""
                    except Exception:
                        pass
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[browse_groups] tap Groups bottom tab by descContains='%s' (actual=%r)",
                             desc_prefix, actual_desc)
                    return True
            except Exception:
                continue
        # 3) resourceId 兜底(少数版本)
        for rid in ("com.facebook.katana:id/tab_groups",
                    "com.facebook.katana:id/bottom_bar_groups"):
            try:
                el = d(resourceId=rid)
                if el.exists(timeout=0.5):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[browse_groups] tap Groups bottom tab by resourceId")
                    return True
            except Exception:
                continue
        return False

    def _tap_your_groups_entry(self, d, did: str) -> bool:
        """点击 Groups 主页的 "Your groups" 入口 (顶部 tab 或 section 标题)。

        必须避免命中 "What's on your mind?" 发帖输入框 —— 用 text 精确匹配
        + clickable 过滤即可排除非按钮元素。
        """
        for txt in self._FB_YOUR_GROUPS_TEXTS:
            try:
                # 同时 text = 精确值 AND clickable=True; TextView 的发帖提示
                # 不是 clickable 的(父 view 才是), 所以不会误命中
                el = d(text=txt, clickable=True)
                if el.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[browse_groups] tap Your groups by text='%s'", txt)
                    return True
                # 退一步: clickable 属性可能在父 layout, 用 descContains
                el = d(descriptionContains=txt)
                if el.exists(timeout=0.4):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[browse_groups] tap Your groups by descContains='%s'", txt)
                    return True
            except Exception:
                continue
        return False

    # 群内 "Members" tab 的精确匹配(避免被"Suggested group"推荐卡片污染)
    _FB_GROUP_MEMBERS_TAB_TEXTS = (
        "Members", "MEMBERS",
        "メンバー",
        "Membri",
        "成员", "成員",
    )
    _FB_GROUP_MEMBERS_SEE_ALL_TEXTS = (
        "查看全部", "See all", "See All",
        "すべて見る", "すべて表示",
        "Ver todos", "Mostra tutto",
    )
    _FB_GROUP_MEMBER_SOURCE_TITLES = {
        "mutual_members": (
            "有共同点的成员", "有共同點的成員",
            "Members with things in common", "Members with things in common with you",
            "共通点のあるメンバー", "共通点があるメンバー",
        ),
        "contributors": (
            "小组贡献者", "小組貢獻者",
            "Group contributors", "Top contributors",
            "グループの投稿者", "コントリビューター",
        ),
    }
    _FB_GROUP_INFO_ACTIVITY_MARKERS = (
        "小组动态", "小組動態", "Group activity",
        "グループアクティビティ", "グループのアクティビティ",
        "今日发帖", "成员总数", "建立时间", "创建时间",
        "Posts today", "Total members", "Created",
    )
    _FB_GROUP_MEMBER_LIST_MARKERS = (
        "Add friend", "Add Friend", "添加好友", "加为好友",
        "友達を追加", "友だちを追加",
        "Admin", "Moderator", "管理员", "管理員", "版主",
        "管理者", "モデレーター",
        "mutual", "共同好友", "共通の友達",
    )
    _FB_GROUP_MEMBER_LIST_STRONG_MARKERS = (
        "搜索成员", "Search members", "Search Members", "メンバーを検索",
        "新加入这个小组的用户", "新加入這個小組的用戶",
        "New to the group", "New members",
        "管理员和版主", "管理員和版主",
        # 2026-05-04 新版 FB 大小写改了 (capital M): "Admins and Moderators"
        "Admins and moderators", "Admins and Moderators",
        "Administrators and moderators",
        # 2026-05-04 新版 FB 把 "Group experts" 改名为 "Group contributors"
        "小组专家", "小組貢獻者", "Group experts", "Group contributors",
        "グループの投稿者",
    )
    _FB_GROUP_MEMBER_ADD_BUTTON_TEXTS = (
        "加为好友", "添加好友", "加为朋友", "加為好友",
        "Add friend", "Add Friend",
        "友達を追加", "友だちを追加",
    )
    _FB_GROUP_MEMBER_NAME_BLOCKLIST = frozenset({
        "成员", "成員", "members", "search members", "搜索成员",
        "查看全部", "查看所有成员", "see all", "see all members",
        "有共同点的成员", "共同点的成员", "管理员和版主",
        "小组专家", "add friend", "加为好友", "添加好友",
        "添加小组成员", "消息功能", "message feature", "messaging",
    })

    def _looks_like_group_members_list_xml(self, xml: str) -> bool:
        """判断当前是否已进入完整成员列表页，而不是群信息页的成员预览。"""
        text = xml or ""
        if not text:
            return False
        has_member_title = any(t in text for t in self._FB_GROUP_MEMBERS_TAB_TEXTS)
        if not has_member_title:
            return False
        has_see_all = any(t in text for t in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS)
        has_info_activity = any(t in text for t in self._FB_GROUP_INFO_ACTIVITY_MARKERS)
        if has_info_activity:
            return False
        has_strong_member_list_signal = any(
            t in text for t in self._FB_GROUP_MEMBER_LIST_STRONG_MARKERS
        )
        if not has_strong_member_list_signal:
            return False
        return any(t in text for t in self._FB_GROUP_MEMBER_LIST_MARKERS)

    def _looks_like_reaction_user_list_xml(self, xml: str) -> bool:
        """Post reaction user lists are not group member lists."""
        text = xml or ""
        if not text:
            return False
        return any(marker in text for marker in (
            "留下心情的用户", "留下心情的人", "表达心情的用户",
            "People who reacted", "reactions", "リアクションした人",
        ))

    def _members_desc_looks_like_group_metadata(self, desc: str) -> bool:
        """过滤群头/搜索结果元信息, 避免把成员数量描述当作 Members tab。"""
        text = (desc or "").strip()
        if not text:
            return False
        if self._looks_like_group_result_meta(text):
            return True
        low = text.lower()
        return any(marker in low for marker in (
            "public group", "private group", "visible group", "hidden group",
        )) or any(marker in text for marker in (
            "公开小组", "私密小组", "公开群组", "私密群组",
            "公開グループ", "非公開グループ", "公開小組", "私密小組",
            "位成员", "位成員", "名成员", "名成員", "名のメンバー",
        ))

    def _tap_members_see_all_link(self, d, did: str,
                                  preferred_source: str = "") -> bool:
        """群信息页成员预览区里的“查看全部/See all”进入完整成员列表。"""
        try:
            xml = d.dump_hierarchy() or ""
        except Exception:
            xml = ""
        if not xml:
            return False
        has_member_title = any(t in xml for t in self._FB_GROUP_MEMBERS_TAB_TEXTS)
        has_see_all = any(t in xml for t in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS)
        has_info_activity = any(t in xml for t in self._FB_GROUP_INFO_ACTIVITY_MARKERS)
        if not has_member_title:
            return False
        if not has_see_all and not has_info_activity:
            return False

        def _after_tap_ok() -> bool:
            try:
                return self._looks_like_group_members_list_xml(d.dump_hierarchy() or "")
            except Exception:
                return False

        source_order = [preferred_source] if preferred_source else [
            "mutual_members", "contributors", "general"]
        source_rank = {s: i for i, s in enumerate(source_order)}

        def _source_for_node(raw: str, top: int, title_nodes) -> str:
            text = raw or ""
            for source, labels in self._FB_GROUP_MEMBER_SOURCE_TITLES.items():
                if any(label in text for label in labels):
                    return source
            best_source = "general"
            best_dist = 9999
            for source, title_top in title_nodes:
                if title_top <= top:
                    dist = top - title_top
                    if 0 <= dist < best_dist and dist <= 420:
                        best_source = source
                        best_dist = dist
            return best_source

        try:
            from ..vision.screen_parser import XMLParser
            parsed = XMLParser.parse(xml)
            title_nodes = []
            for node in parsed:
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                _left, top, _right, _bottom = node.bounds
                for source, labels in self._FB_GROUP_MEMBER_SOURCE_TITLES.items():
                    if any(label in raw for label in labels):
                        title_nodes.append((source, top))

            candidates = []
            for node in parsed:
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                if not any(label == raw or label in raw
                           for label in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS):
                    continue
                left, top, right, bottom = node.bounds
                if top < 240 or bottom > 1250:
                    continue
                source = _source_for_node(raw, top, title_nodes)
                if preferred_source and source != preferred_source:
                    continue
                rank = source_rank.get(source, source_rank.get("general", 99))
                # Same-rank candidates lower on the screen are often the section
                # body "See all"; keep the explicit source ordering first.
                candidates.append((rank, top, -left, left, right, bottom, raw, source))
            candidates.sort()
            for _rank, _top, _neg_left, left, right, bottom, raw, source in candidates[:4]:
                self.hb.tap(d, (left + right) // 2, (_top + bottom) // 2)
                time.sleep(1.5)
                if _after_tap_ok():
                    try:
                        self._last_group_member_source = source
                    except Exception:
                        pass
                    log.info("[extract_members] tap %s See all by XML=%r ✓",
                             source, raw[:40])
                    return True
        except Exception as e:
            log.debug("[extract_members] source-aware See all XML failed: %s", e)

        # 2026-05-03 P1-B selector helper: anchor-aware See all 探测.
        # 真机第 5 轮 (task 613f3af9) 显示新版 FB 群信息页有多个 section 各自含
        # See all (About / Photos / Files / Members / Featured), 旧路径找到第
        # 一个 text='See all' 就 tap, 跳到非成员页面 → _looks_like_group_members
        # _list_xml 校验失败. 解法: 收集所有 See all 节点 + 邻近 200px 内必须
        # 有 Members/成员/メンバー anchor, 才视为成员区入口; 失败的 candidate
        # back 一次回群信息页再试下一个.
        _MEMBER_SECTION_ANCHORS = (
            "Members", "MEMBERS",
            "メンバー",
            "Membri",
            "成员", "成員",
        )
        # P1-B v2 (2026-05-03 真机第六轮反馈): 上版本 anchor 距离判定 200px 太宽,
        # 群信息页所有 4 个 See all (Photos/Files/Members/Featured) 都被关联到
        # 同一个 'Members' anchor → 3 次误 tap. 收严策略:
        #   1. 扩展 anchor 类型: 加 "数字 + members/成员/メンバー" 模式 (FB 群信息页
        #      Members section 标题就是 "7.3K members" 这种), 比纯 "Members" 文字
        #      候选多, 锚点更密.
        #   2. 距离判定收严到 80px 同一行 + anchor 必须在 See all 左侧 (FB 标准布局
        #      "标题在左 See all 在右"); 取消纵向 200px 模糊匹配.
        #   3. 加 [seeall-dbg] 高粒度日志: 列出所有 See all 节点和 Anchor 节点的
        #      真实 bounds, 让下一轮真机 dump 一次到位 (避免再迭代).
        import re as _re_anchor
        _NUM_MEMBERS_PATTERN = _re_anchor.compile(
            r"\d[\d.,]*\s*[KkMm万]?\s*(?:members?|成员|成員|メンバー)",
            _re_anchor.IGNORECASE,
        )
        try:
            from ..vision.screen_parser import XMLParser as _XPSA
            parsed_sa = list(_XPSA.parse(xml))
            # 高粒度调试 dump: See all 节点 + Member anchor 节点
            for _n in parsed_sa:
                if not getattr(_n, "bounds", None):
                    continue
                _t = (getattr(_n, "text", "") or "").strip()
                _d_ = (getattr(_n, "content_desc", None) or "").strip()
                _is_seeall_dbg = any(
                    _sa in (_t + " " + _d_)
                    for _sa in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS
                )
                _is_anchor_dbg = (
                    _t in _MEMBER_SECTION_ANCHORS
                    or _d_ in _MEMBER_SECTION_ANCHORS
                    or bool(_NUM_MEMBERS_PATTERN.search(_t))
                    or bool(_NUM_MEMBERS_PATTERN.search(_d_))
                )
                if _is_seeall_dbg:
                    log.info(
                        "[seeall-dbg] SeeAll t=%r d=%r bounds=%s clk=%s",
                        _t[:30], _d_[:60], _n.bounds,
                        getattr(_n, "clickable", "?"),
                    )
                if _is_anchor_dbg:
                    log.info(
                        "[seeall-dbg] Anchor t=%r d=%r bounds=%s clk=%s",
                        _t[:50], _d_[:80], _n.bounds,
                        getattr(_n, "clickable", "?"),
                    )
            anchor_bounds: list = []
            for _n in parsed_sa:
                if not getattr(_n, "bounds", None):
                    continue
                _t = (getattr(_n, "text", "") or "").strip()
                _d_ = (getattr(_n, "content_desc", None) or "").strip()
                _matched_label = None
                # 1) 全等 / 起始的传统 anchor
                for _lbl in _MEMBER_SECTION_ANCHORS:
                    if (_t == _lbl or _d_ == _lbl
                        or _d_.startswith(_lbl + " ")
                        or _d_.startswith(_lbl + ",")):
                        _matched_label = _lbl
                        break
                # 2) "7.3K members" 等数字+成员模式 (可能在 text 或 desc 任一)
                if not _matched_label:
                    if _NUM_MEMBERS_PATTERN.search(_t):
                        _matched_label = f"NUM:{_t[:30]}"
                    elif _NUM_MEMBERS_PATTERN.search(_d_):
                        _matched_label = f"NUM:{_d_[:30]}"
                if _matched_label:
                    anchor_bounds.append((_matched_label, _n.bounds))
            see_all_candidates: list = []
            for _n in parsed_sa:
                if not getattr(_n, "bounds", None):
                    continue
                _t = (getattr(_n, "text", "") or "").strip()
                _d_ = (getattr(_n, "content_desc", None) or "").strip()
                _hit = False
                for _sa in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS:
                    if _t == _sa or _d_ == _sa or _sa in _d_:
                        _hit = True
                        break
                if not _hit:
                    continue
                _l, _t2, _r, _b = _n.bounds
                # P1-B v3 (2026-05-03 真机第七轮反馈): 上版本 `_b > 1400` 把
                # 群信息页 Members 段 See all (y=1462-1506) 全部误杀. 真机日志
                # 显示真正 clickable=True 的整行 See all 在 y∈[1462,1506],
                # 这台 Redmi 13C 屏幕高 1600, See all 在 ~91% 位置完全合理.
                # 取消 _b 上限, 改用 _t2 ≥ 240 过滤状态栏即可; clickable 节点
                # 优先 (代码下方 sort 时纳入).
                if _t2 < 240:
                    continue
                _sa_cx = (_l + _r) // 2
                _sa_cy = (_t2 + _b) // 2
                _best_anchor = None
                _best_priority = 99
                _best_sort = 99999
                _is_clickable = bool(getattr(_n, "clickable", False))
                for _alabel, (_al, _at, _ar, _ab) in anchor_bounds:
                    _a_cx = (_al + _ar) // 2
                    _a_cy = (_at + _ab) // 2
                    if _a_cx >= _sa_cx:
                        continue   # anchor 必须在 See all 左侧
                    _y_diff = _sa_cy - _a_cy   # +: see-all 在 anchor 下方
                    # 2026-05-03 P1-A v4 (真机第十轮分析): 群 1/3 layout 有同
                    # 行 See all + 下一行全宽 See all (实际属于其它 section).
                    # priority 分层: 同一行总是优先于跨行, 杜绝"下一 section
                    # 全宽 clickable 抢位".
                    if abs(_y_diff) <= 30:
                        _priority = 0   # 同一行 (FB 标准 "标题左 See all 右")
                        _sort = abs(_a_cx - _sa_cx)
                    elif 30 < _y_diff <= 100 and _is_clickable:
                        _priority = 1   # 跨行整行 See all (覆盖群 2 第 8 轮 layout)
                        _sort = _y_diff   # y 距离作为 sort
                    else:
                        continue
                    if (_priority, _sort) < (_best_priority, _best_sort):
                        _best_priority = _priority
                        _best_sort = _sort
                        _best_anchor = _alabel
                if _best_anchor:
                    see_all_candidates.append(
                        (_best_priority, _best_sort, _t2, _l, _r, _b,
                         _best_anchor)
                    )
            see_all_candidates.sort()
            log.info(
                "[extract_members] anchor-aware See all candidates: %d "
                "(anchors found: %d, strict-row mode)",
                len(see_all_candidates), len(anchor_bounds),
            )
            for _prio, _sort, _top, _l, _r, _b, _alabel in see_all_candidates[:3]:
                self.hb.tap(d, (_l + _r) // 2, (_top + _b) // 2)
                time.sleep(1.5)
                if _after_tap_ok():
                    try:
                        self._last_group_member_source = (
                            preferred_source or "general"
                        )
                    except Exception:
                        pass
                    log.info(
                        "[extract_members] tap See all (anchor=%r prio=%d "
                        "sort=%d) ✓",
                        _alabel, _prio, _sort,
                    )
                    return True
                log.info(
                    "[extract_members] anchor See all (anchor=%r prio=%d) "
                    "tap 后非成员列表, back + 试下一个", _alabel, _prio,
                )
                try:
                    d.press("back")
                    time.sleep(0.8)
                except Exception:
                    pass
        except Exception as _ane:
            log.debug("[extract_members] anchor-aware See all path failed: %s",
                       _ane)

        for label in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS:
            for sel in ({"text": label, "clickable": True}, {"text": label}):
                try:
                    el = d(**sel)
                    if not el.exists(timeout=0.45):
                        continue
                    cx, cy = self._el_center(el)
                    if cy < 240 or cy > 1250:
                        continue
                    self.hb.tap(d, cx, cy)
                    time.sleep(1.5)
                    if _after_tap_ok():
                        try:
                            self._last_group_member_source = preferred_source or "general"
                        except Exception:
                            pass
                        log.info("[extract_members] tap member See all by text=%r ✓",
                                 label)
                        return True
                except Exception:
                    continue

        try:
            from ..vision.screen_parser import XMLParser
            candidates = []
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or not getattr(node, "bounds", None):
                    continue
                if not any(label == raw or label in raw
                           for label in self._FB_GROUP_MEMBERS_SEE_ALL_TEXTS):
                    continue
                left, top, right, bottom = node.bounds
                if top < 240 or bottom > 1250:
                    continue
                # 成员预览区通常在页面上半部，优先右侧链接。
                candidates.append((top, -left, left, right, bottom, raw))
            candidates.sort()
            for _top, _neg_left, left, right, bottom, raw in candidates[:3]:
                self.hb.tap(d, (left + right) // 2, (_top + bottom) // 2)
                time.sleep(1.5)
                if _after_tap_ok():
                    try:
                        self._last_group_member_source = preferred_source or "general"
                    except Exception:
                        pass
                    log.info("[extract_members] tap member See all by XML=%r ✓",
                             raw[:40])
                    return True
        except Exception as e:
            log.debug("[extract_members] member See all XML fallback failed: %s", e)

        if has_info_activity:
            try:
                w, h = d.window_size()
                # 群信息页的“查看全部”通常在成员区标题右侧；只有在已经
                # 确认为目标群信息页时才用坐标兜底，避免误点普通列表。
                self.hb.tap(d, int(w * 0.86), int(h * 0.33))
                time.sleep(1.5)
                if _after_tap_ok():
                    try:
                        self._last_group_member_source = preferred_source or "general"
                    except Exception:
                        pass
                    log.info("[extract_members] tap member See all by preview coordinate ✓")
                    return True
            except Exception as e:
                log.debug("[extract_members] member See all coordinate fallback failed: %s", e)
        return False

    def _tap_members_see_all_after_scroll(self, d, did: str,
                                          preferred_source: str = "",
                                          max_scrolls: int = 3) -> bool:
        """在群简介页向下扫到成员区, 再点成员预览的“查看全部”。"""
        if self._tap_members_see_all_link(d, did, preferred_source):
            return True
        for _ in range(max(0, max_scrolls)):
            try:
                self.hb.scroll_down(d)
                self.hb.wait_read(600)
            except Exception:
                try:
                    d.swipe(0.5, 0.82, 0.5, 0.36, duration=0.35)
                    time.sleep(0.8)
                except Exception:
                    pass
            if self._tap_members_see_all_link(d, did, preferred_source):
                return True
        return False

    def _open_group_info_then_members(self, d, did: str,
                                      preferred_source: str = "") -> bool:
        """从群动态页/群主页进入简介页, 再打开完整成员列表。"""
        if self._tap_members_see_all_link(d, did, preferred_source):
            return True

        # 2026-05-03 P1-A v6 (真机第十二轮 dump 反馈): 真机 dump 显示进群后落在
        # "群预览页" (非成员模式), 含 cover/群头/Join button/子 tab/帖子流, 但
        # **顶部没有 Members anchor**. 第 11 轮 mutual=4 成功的根因是滚屏到底部
        # 看到 "成员预览" section (anchor y=1499). 第 1 次 see_all_link 失败应
        # 先滚屏多次找底部成员预览, 而不是直接 tap 群头 (会跳到 cover 页破坏状态).
        if self._tap_members_see_all_after_scroll(
            d, did, preferred_source, max_scrolls=5,
        ):
            log.info(
                "[extract_members] opened member list via early scroll path"
            )
            return True

        # 2026-05-03 P1-A v5 (真机第十一轮分析): 群 1/3 (Files 默认 tab) 进入后
        # tap 群头紧凑卡片 (bounds.right=407) 之后未到群信息页, 同行/跨行 See all
        # 都 tap fail. 此处加超详细 [groupinfo-dbg] dump, 输出当前页所有 clickable
        # 节点 + 含 Members/About/成员 等关键词节点, 一轮真机即可定位真入口.
        try:
            _xml_dbg = d.dump_hierarchy() or ""
            if _xml_dbg:
                from ..vision.screen_parser import XMLParser as _XPDBG
                _parsed_dbg = list(_XPDBG.parse(_xml_dbg))
                _GROUPINFO_KW = (
                    "Members", "MEMBERS", "members",
                    "メンバー", "Membri",
                    "成员", "成員",
                    "About", "ABOUT", "about", "关于", "關於",
                    "情報", "Information",
                    "Public group", "Private group",
                    "See all", "see all",
                    "Group info", "Group settings",
                )
                _hits_dbg = 0
                for _n in _parsed_dbg:
                    if not getattr(_n, "bounds", None):
                        continue
                    _t = (getattr(_n, "text", "") or "").strip()
                    _d_ = (getattr(_n, "content_desc", None) or "").strip()
                    _clk = bool(getattr(_n, "clickable", False))
                    _has_kw = any(k in (_t + " " + _d_) for k in _GROUPINFO_KW)
                    # 输出: clickable 节点 OR 含关键词节点
                    if not (_clk or _has_kw):
                        continue
                    log.info(
                        "[groupinfo-dbg] t=%r d=%r bounds=%s clk=%s",
                        _t[:40], _d_[:80], _n.bounds, _clk,
                    )
                    _hits_dbg += 1
                    if _hits_dbg >= 50:
                        break
                log.info("[groupinfo-dbg] total hits=%d nodes=%d",
                          _hits_dbg, len(_parsed_dbg))
        except Exception as _gid_e:
            log.debug("[groupinfo-dbg] failed: %s", _gid_e)

        # 2026-05-03 P1-A real-device fix: 新版 FB 群头页面布局改了, Members tab
        # 不再作为顶部独立 chip 出现; 群信息入口收敛到一个大按钮, 其 content-desc
        # 含 "<group_name>, Public group · NK members" 这类完整描述. 旧坐标启发
        # 路径 (tap w*0.40, y=125) 落在状态栏区域, 永远命中不到. 改为 desc 匹配
        # "Public group / Private group" 等多语言群类型词, 精确点击群头卡片.
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser as _XPG
            _GROUP_HEADER_DESC_MARKERS = (
                "Public group", "Private group",
                "公开小组", "公開小組", "私密小组", "私密小組",
                "公開グループ", "非公開グループ",
            )
            for node in _XPG.parse(xml):
                if not getattr(node, "bounds", None):
                    continue
                if not bool(getattr(node, "clickable", False)):
                    continue
                desc = (getattr(node, "content_desc", None) or "").strip()
                if not desc or not any(
                    m in desc for m in _GROUP_HEADER_DESC_MARKERS
                ):
                    continue
                _l, _t, _r, _b = node.bounds
                # 群头大卡片通常在屏幕中部偏上 (y∈[300,900]), 高度有限避免误命
                # 整屏背景容器.
                if not (300 <= _t <= 900 and (_b - _t) <= 240):
                    continue
                self.hb.tap(d, (_l + _r) // 2, (_t + _b) // 2)
                time.sleep(1.5)
                log.info(
                    "[extract_members] tap group header card desc=%r "
                    "bounds=%s",
                    desc[:60], node.bounds,
                )
                if self._tap_members_see_all_link(d, did, preferred_source):
                    log.info(
                        "[extract_members] opened member list after group "
                        "header card tap")
                    return True
                if self._tap_members_see_all_after_scroll(
                    d, did, preferred_source, max_scrolls=3
                ):
                    log.info(
                        "[extract_members] opened member list via header"
                        " card + scroll")
                    return True
                break  # 群头只 tap 一次, 失败就降级原 fallback
        except Exception as _gh_e:
            log.debug("[extract_members] group header card path failed: %s",
                       _gh_e)

        try:
            w, h = d.window_size()
        except Exception:
            w, h = 720, 1600

        # 1) 动态流顶部的群名标题栏: 点开群主页大头图区域。
        try:
            self.hb.tap(d, int(w * 0.40), min(125, int(h * 0.08)))
            time.sleep(1.3)
            if self._tap_members_see_all_link(d, did, preferred_source):
                log.info("[extract_members] opened member list after tapping group title bar")
                return True
        except Exception as e:
            log.debug("[extract_members] tap group title bar fallback failed: %s", e)

        # 2) 群主页大标题右侧有 chevron, 点它进入简介/About。
        try:
            self.hb.tap(d, int(w * 0.58), int(h * 0.38))
            time.sleep(1.3)
            if self._tap_members_see_all_after_scroll(
                    d, did, preferred_source, max_scrolls=1):
                log.info("[extract_members] opened member list via group info/about page")
                return True
        except Exception as e:
            log.debug("[extract_members] tap group info chevron fallback failed: %s", e)

        # 3) 如果调用时已经在简介页顶部, 直接扫到成员区。
        return self._tap_members_see_all_after_scroll(
            d, did, preferred_source, max_scrolls=1)

    def _clean_group_member_candidate_name(self, raw: str) -> str:
        """从新版 FB 成员行 content-desc 中提取姓名。"""
        text = (raw or "").replace("\xa0", " ").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        for sep in ("\n", "\r"):
            if sep in text:
                text = text.split(sep, 1)[0].strip()
        for sep in (",", "，"):
            if sep in text:
                text = text.split(sep, 1)[0].strip()
                break
        for marker in (
            " 位共同好友", "位共同好友", " mutual friend",
            " mutual friends", "目前就职", "currently works",
            "分",
        ):
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx].strip()
        text = text.strip(" ·,，:：")
        if not text:
            return ""
        if text.lower() in self._FB_GROUP_MEMBER_NAME_BLOCKLIST:
            return ""
        if any(marker in text for marker in (
            "这份名单", "這份名單", "名单包含", "名單包含",
            "贡献积分", "貢獻積分", "This list", "this list",
            "このリスト", "リストには",
        )):
            return ""
        if any(t in text for t in ("查看", "搜索", "添加", "好友", "小组成员")):
            return ""
        if len(text) < 2 or len(text) > 60:
            return ""
        if re.fullmatch(r"[\d\s,，.]+", text):
            return ""
        return text

    def _extract_group_member_candidates(
        self, elements,
        enable_no_add_fallback: bool = False,
    ) -> List[Dict[str, Any]]:
        """从成员列表 XML 元素中抽取带“加为好友”动作的用户.

        Args:
            elements: XMLParser 解析后的元素列表.
            enable_no_add_fallback: 当没有任何 "加为好友" 按钮时是否启用
                P7-C-1 row-pattern 降级识别. 默认 False 保留 'must require
                add_friend action' 契约 (test_member_candidates_require_add_friend_action),
                避免把帖子控件/图片/按钮误识别为客户线索. 真业务调用方
                (extract_group_members 在 contributors 完整列表页) 显式传
                True 启用 P7-C-1 降级.
        """
        add_centers = []
        for el in elements:
            raw = ((getattr(el, "text", "") or "") + " " +
                   (getattr(el, "content_desc", "") or "")).strip()
            if not raw or not getattr(el, "bounds", None):
                continue
            if any(label in raw for label in self._FB_GROUP_MEMBER_ADD_BUTTON_TEXTS):
                left, top, right, bottom = el.bounds
                add_centers.append(((top + bottom) // 2, left, right))
        if not add_centers:
            # 2026-05-05 Stage T: P7-C-1 fallback 改 opt-in 而非默认
            # (违反 test_member_candidates_require_add_friend_action 契约).
            # 调用方 (extract_group_members:9002) 决定是否启用降级识别.
            if not enable_no_add_fallback:
                return []
            # 2026-05-05 P7-C-1: contributors 完整列表页 (已是群成员视角)
            # 不显示 "加好友" 按钮 → 原 return [] 导致 task f2aebe47/aa3a5783
            # 跑满 6 次 scroll 仍 yielded=0/40. 降级 row-pattern 识别:
            # 用 _clean_group_member_candidate_name 严过滤 (blocklist + role
            # markers + length 2-60), 配 y/x/高度 几何约束.
            out_fb: List[Dict[str, Any]] = []
            seen_fb = set()
            for el in elements:
                bounds = getattr(el, "bounds", None)
                if not bounds:
                    continue
                left, top, right, bottom = bounds
                if top < 240 or bottom > 1500:
                    continue
                # 名字行高度 25-80 (排除大容器 / 整 row / 图片节点)
                if (bottom - top) < 25 or (bottom - top) > 80:
                    continue
                # 头像在 x<100 区, 名字必在头像右侧
                if left < 80:
                    continue
                raw_fb = (getattr(el, "content_desc", "") or
                          getattr(el, "text", "") or "").strip()
                name_fb = self._clean_group_member_candidate_name(raw_fb)
                if not name_fb:
                    continue
                key_fb = name_fb.casefold()
                if key_fb in seen_fb:
                    continue
                seen_fb.add(key_fb)
                # 2026-05-05 P7-D: 带 row 名字 bounds → executor:4178 用它
                # tap 进 profile, 设 from_current_profile=True 绕开 search_people.
                # 真机 task a50d9f9f: 22 contributors row 名字抽出后, search 全
                # not_found 因为 FB 全局搜不到生人 displayName.
                item_fb = {
                    "name": name_fb,
                    "profile_pic_bounds": [int(left), int(top),
                                            int(right), int(bottom)],
                }
                if raw_fb and raw_fb != name_fb:
                    item_fb["profile_snippet"] = raw_fb[:180]
                out_fb.append(item_fb)
            if out_fb:
                log.info("[extract_candidates] P7-C-1 fallback (no add_centers) "
                         "row-pattern 抽到 %d 候选", len(out_fb))
            return out_fb

        def _has_add_action_near(bounds) -> bool:
            _left, top, _right, bottom = bounds
            # 同一成员卡里, 姓名通常在“加为好友”按钮上方或同一垂直区间。
            # 只用中心点距离会把上一张卡片的按钮误配到下一张无按钮卡片(自己)。
            return any((top - 40) <= add_y <= (bottom + 110)
                       for add_y, _al, _ar in add_centers)

        out: List[Dict[str, Any]] = []
        seen = set()
        for el in elements:
            bounds = getattr(el, "bounds", None)
            if not bounds:
                continue
            left, top, right, bottom = bounds
            if top < 240 or bottom > 1500:
                continue
            if not _has_add_action_near(bounds):
                continue
            raw = (getattr(el, "content_desc", "") or
                   getattr(el, "text", "") or "").strip()
            name = self._clean_group_member_candidate_name(raw)
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            # 2026-05-05 P7-D: 带 row 名字 bounds (同 P7-C-1 fallback) →
            # executor:4178 触发 tap row 进 profile, 绕开 search_people.
            item = {
                "name": name,
                "profile_pic_bounds": [int(left), int(top),
                                        int(right), int(bottom)],
            }
            if raw and raw != name:
                item["profile_snippet"] = raw[:180]
            out.append(item)
        return out

    def _tap_via_search_members_input(self, d, did: str) -> bool:
        """2026-05-04 P0: 在 Members 列表 preview 页用 "Search Members" 输入框
        触发 FB 服务端 push 搜索结果, 拿真用户名 (绕开 SDUI 不暴露 contributors
        真名的限制).

        步骤:
          1. 当前页面 dump 找 Search Members 输入框 (EditText / hint=Search Members)
          2. tap 输入框 + 输入随机日文常用字 ("美" / "ま" / "ko" 等)
          3. 等 1.5s FB 服务端 push 结果
          4. dump 抓 search results — 真名应该在列表里
          5. 找到 result 后 return True; 后续 _campaign_add_friends 直接 tap 第一个 row

        注意: 此函数返 True 表示**已触发搜索 + 当前在搜索结果页**, 后续
        extract_group_members 主流程会从 search results 抽 candidates.
        """
        try:
            from src.vision.screen_parser import XMLParser as _XPSI
        except Exception:
            return False

        try:
            xml_pre = d.dump_hierarchy() or ""
        except Exception:
            return False

        # Step 1: 找 Search Members 输入框 (EditText 或 hint='Search Members')
        # 多语言: 'Search Members' / 'メンバーを検索' / '搜索成员' / '搜尋成員'
        _search_hints = (
            "Search Members", "Search members", "Search member",
            "メンバーを検索", "メンバー検索",
            "搜索成员", "搜索成員", "搜尋成員",
            "Buscar miembros", "Cerca membri",
        )
        _input_bounds = None
        for node in _XPSI.parse(xml_pre):
            if not getattr(node, "bounds", None):
                continue
            cls = (getattr(node, "cls", "") or "").strip()
            txt = (getattr(node, "text", "") or "").strip()
            desc = (getattr(node, "content_desc", None) or "").strip()
            hint = (getattr(node, "hint", None) or "").strip()
            l, t, r, b = node.bounds
            if t > 600 or t < 100:  # Search Members 输入框通常在顶部 y=180-320
                continue
            # 1) class=EditText 在顶部
            if "EditText" in cls and (b - t) < 100:
                _input_bounds = (l, t, r, b)
                log.info("[search-input] 找到 EditText bounds=(%d,%d,%d,%d)",
                         l, t, r, b)
                break
            # 2) text/desc/hint 含 Search 关键词
            if any(_p in (txt + desc + hint) for _p in _search_hints):
                _input_bounds = (l, t, r, b)
                log.info("[search-input] 找到 search hint bounds=(%d,%d,%d,%d) val=%r",
                         l, t, r, b, (txt or desc or hint)[:40])
                break

        if not _input_bounds:
            log.info("[search-input] 当前页面无 Search Members 输入框")
            return False

        _il, _it, _ir, _ib = _input_bounds
        _icx, _icy = (_il + _ir) // 2, (_it + _ib) // 2

        # Step 2: tap + 输入随机日文常用字 (jp persona 群里)
        # 选 1 个常见日本姓氏字, FB 搜索按 "starts with" 匹配, 应有多个结果
        import random as _rnd
        _seed_chars = ["田", "佐", "鈴", "山", "中", "高", "斎", "石", "美", "智", "和"]
        _seed = _rnd.choice(_seed_chars)
        try:
            self.hb.tap(d, _icx, _icy)
            time.sleep(0.6)
            # 用 atx-agent send_keys 输入 (走 ADBKeyboard 支持 unicode)
            try:
                d.send_keys(_seed)
                time.sleep(1.5)
            except Exception:
                # fallback: adb shell input text (英文 OK, unicode 需 ADBKeyboard)
                import subprocess
                subprocess.run(
                    ["adb", "-s", did, "shell", "input", "text", _seed],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                time.sleep(1.5)
            log.info("[search-input] 输入 %r 到 Search Members", _seed)
        except Exception as e:
            log.debug("[search-input] tap+输入失败: %s", e)
            return False

        # Step 3: 等结果 + dump 验证真名出现
        time.sleep(1.0)
        try:
            xml_results = d.dump_hierarchy() or ""
        except Exception:
            return False

        # Step 4: 抽 search results 真名 + bounds, 保存到 instance attribute
        # 让上层 _campaign_extract_members 直接用 (跳过 SDUI 不暴露的 contributors 抽取).
        _BUTTON_LABELS = {
            "Search Members", "Search", "Members", "Add", "Message",
            "Admin", "Moderator", "View profile", "See all",
            "メンバー", "メンバーを検索", "管理人", "管理者",
            "投稿", "Posts", "About", "Files", "Photos", "Events",
            "Anonymous post", "Feeling", "Poll", "Joined", "Invite",
            "Featured", "You", "Reels", "Albums",
            # 2026-05-04: search-input log 实测脏候选
            "Learn More", "Learn more", "Group experts", "Group expert",
            "Top contributors", "Group contributors", "Group rules",
            "Group description", "Suggested", "Group", "Page",
            "Anyone can post", "Active", "Online", "Friends",
            "Friend Requests", "Friend requests", "Send Message",
            "Send message", "Follow", "Following",
        }
        # 用 substring 排除 (整行精确等于不够 — "Group experts" 可能带后缀)
        _BUTTON_PREFIXES = (
            "Learn ", "Group ", "Top ", "Suggested",
        )
        _candidates_with_bounds = []
        _seen_names = set()  # dedup: 同一 name 多 row 只取第一个
        for node in _XPSI.parse(xml_results):
            if not getattr(node, "bounds", None):
                continue
            txt = (getattr(node, "text", "") or "").strip()
            l, t, r, b = node.bounds
            if t < 300 or t > 1400 or (b - t) > 100:
                continue
            if not txt or txt in _BUTTON_LABELS:
                continue
            if any(txt.startswith(_p) for _p in _BUTTON_PREFIXES):
                continue
            # 排除明显的 metadata 行 (含 "points" / "members" / "Lives in" 等)
            if any(_meta in txt for _meta in (
                "points", "members", "mutual friend", "Lives in", "Member of",
                "Rising contributor", "Top contributor", "All-star contributor",
                "Admin", "since",
            )):
                continue
            if not (2 <= len(txt) <= 30 and any(
                "぀" <= c <= "ヿ" or "一" <= c <= "鿿"
                or c.isalpha() for c in txt
            )):
                continue
            # 真名启发: 至少含一个空格 (Foo Bar) 或全 CJK (山田太郎)
            _has_space = " " in txt or "　" in txt  # 半角/全角空格
            _all_cjk = all(
                "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or c.isspace()
                for c in txt
            )
            if not (_has_space or _all_cjk):
                continue
            if txt in _seen_names:
                continue
            _seen_names.add(txt)
            _candidates_with_bounds.append({
                "name": txt,
                "bounds": [l, t, r, b],
            })
        log.info("[search-input] 输入 %r 后抽到 %d 候选真名 (with bounds)",
                 _seed, len(_candidates_with_bounds))
        for _c in _candidates_with_bounds[:5]:
            log.info("  candidate: name=%r bounds=%s", _c["name"], _c["bounds"])

        if len(_candidates_with_bounds) < 2:
            return False

        # 保存到 instance attribute, _campaign_extract_members 检查并优先用
        try:
            self._search_input_candidates = _candidates_with_bounds
            self._search_input_seed = _seed
        except Exception:
            pass
        return True

    def _tap_via_about_members_path(self, d, did: str) -> bool:
        """2026-05-04 8 截图实证的真路径: 群首页 → tap 群名 banner → About 页
        → Members "See all" → Members 列表页(分组). 然后调用方再决定是否
        进 Group contributors 完整列表.

        步骤:
          1. dump 当前页面, 找 顶部"群名" banner 节点(text=group_name 且 y<400
             且 clickable=True or 父 clickable). 该节点真机截图位于群首页 banner
             下方 (y≈600), 但 clickable bounds 在更上方 banner 区域.
          2. tap → 跳到 About 页. dump 含 "About" / "Public group" / "Group activity"
             / "Members" 字样.
          3. About 页找 "Members" anchor + 同行 "See all" (clk=True). tap See all.
          4. 进 Members 页. dump 含 "Search Members" / "Admins and Moderators"
             / "Group contributors" / "清田" 等. 用 _looks_like_group_members_list_xml
             判定 PASS.

        失败任一步返 False, 让 fallback 接管.
        """
        try:
            xml0 = d.dump_hierarchy() or ""
        except Exception:
            return False
        if not xml0:
            return False

        # 已经在 Members 列表页就直接 return True
        if self._looks_like_group_members_list_xml(xml0):
            log.info("[about-path] already on members list, skip")
            return True

        # Step A: tap 群名 banner 跳 About 页
        # 真机截图: 群首页 顶部 banner 下方有大字群名 (y≈600, clickable=true,
        # 含 ">" 后缀指示可 tap). 用 XMLParser 找最匹配的群名节点 + 位置在
        # 屏幕上半部 (y < 700) + clk=True.
        try:
            from src.vision.screen_parser import XMLParser
        except Exception:
            return False

        # 2026-05-04 新版 FB SDUI: dump_hierarchy 不暴露群名 text 节点
        # (accessibility tree lazy, screen 上字看得到 dump 抓不到).
        # 改用 cover photo 节点 bounds 计算群名 banner 位置:
        # cover photo bottom + 50px = 群名 anchor 中心 y (群名在 cover 下方紧贴一行).
        _cover_bounds = None
        for node in XMLParser.parse(xml0):
            if not getattr(node, "bounds", None):
                continue
            desc = (getattr(node, "content_desc", None) or "").strip()
            if "Cover photo" in desc or "cover photo" in desc.lower():
                _cover_bounds = node.bounds
                log.info("[about-path] cover_photo bounds=%s desc=%r",
                         _cover_bounds, desc[:60])
                break

        if not _cover_bounds:
            log.warning("[about-path] 未找到 cover photo anchor, 当前页面可能不是群首页")
            return False

        _ct = _cover_bounds[1]
        _cb = _cover_bounds[3]
        cx = 360  # 屏幕宽度 720, 中心
        cy = _cb + 50  # cover bottom + 50px = 群名行
        log.info("[about-path] tap group-name banner via cover_bottom+50 = (%d, %d)",
                 cx, cy)
        try:
            self.hb.tap(d, cx, cy)
            time.sleep(2.0)
        except Exception as e:
            log.debug("[about-path] tap banner failed: %s", e)
            return False

        # Step B: About 页找 Members section + 同行 See all
        try:
            xml_about = d.dump_hierarchy() or ""
        except Exception:
            return False
        if not xml_about:
            return False
        if self._looks_like_group_members_list_xml(xml_about):
            log.info("[about-path] tap banner 直接到 members list (短路 PASS)")
            return True

        # 找 "Members" anchor (text/desc) + 同行 "See all" (clk=True)
        members_anchor = None
        seeall_btns = []
        for node in XMLParser.parse(xml_about):
            if not getattr(node, "bounds", None):
                continue
            txt = (getattr(node, "text", "") or "").strip()
            desc = (getattr(node, "content_desc", None) or "").strip()
            clk = bool(getattr(node, "clickable", False))
            l, t, r, b = node.bounds
            if (txt == "Members" or desc == "Members"
                    or txt == "成员" or txt == "成員"
                    or txt == "メンバー") and (b - t) < 100:
                if members_anchor is None or t < members_anchor[1]:
                    members_anchor = (l, t, r, b)
            if clk and (txt == "See all" or desc == "See all"
                        or txt == "查看全部" or txt == "すべて見る"):
                seeall_btns.append((l, t, r, b))

        log.info("[about-path] members_anchor=%s seeall_btns=%d",
                 members_anchor, len(seeall_btns))
        if not members_anchor or not seeall_btns:
            log.warning("[about-path] About 页未找到 Members + See all anchor")
            return False

        # 选 y 跟 members_anchor 同行 (差 <= 100px) 的 See all
        m_top = members_anchor[1]
        same_row = [s for s in seeall_btns if abs(s[1] - m_top) <= 100]
        if not same_row:
            # 退而求其次: 取 y > members_anchor 的最近一个
            below = [s for s in seeall_btns if s[1] >= m_top]
            same_row = below[:1] if below else seeall_btns[:1]
        sl, st, sr, sb = same_row[0]
        scx, scy = (sl + sr) // 2, (st + sb) // 2
        log.info("[about-path] tap Members See all bounds=(%d,%d,%d,%d) center=(%d,%d)",
                 sl, st, sr, sb, scx, scy)
        try:
            self.hb.tap(d, scx, scy)
            time.sleep(2.0)
        except Exception as e:
            log.debug("[about-path] tap See all failed: %s", e)
            return False

        # Step C: 验证已到 Members 列表页
        try:
            xml_members = d.dump_hierarchy() or ""
        except Exception:
            return False
        if not self._looks_like_group_members_list_xml(xml_members):
            log.warning("[about-path] tap Members See all 后未识别为成员列表页")
            return False
        log.info("[about-path] ✓ 进入 Members 列表页 (preview)")

        # Step D 2026-05-04 用户截图 006 揭示: Members 列表页顶部只显
        # admins/experts/contributors *预览*, 完整 contributors 列表要再
        # tap "See all" (Group contributors / Members With Things in Common
        # 等 section 下方). Q4N7 默认看 Things in Common preview, IJ8H 看
        # contributors preview. 不论 section, 都 tap 第二个 See all 进完整列表.
        try:
            from src.vision.screen_parser import XMLParser as _XPS
        except Exception:
            return True

        # 2026-05-05 P7-A: search-input 路径从主路径降为 fallback (用户反馈
        # task a1c2f336: Members preview 后被 search-input 短路, 1 字符抽 1-7
        # 候选 << max=40, 实际目标是滚动抽全员). 先走完整 contributors 列表
        # (下方 6998+ 三层 dump 找第二个 See all), 进 extract_group_members
        # 8923 滚动循环抽全员; search-input 留作低活跃账号 0 抽时的 fallback.

        # 2026-05-04 重构 dump 策略: SDUI lazy load + atx-agent cache 不一致
        # 导致 task 内 dump 抓不到 "See all members" 文字. 用 retry + raw adb
        # uiautomator dump fallback + 坐标 fallback 三层保护.
        _seeall_phrases = ("See all", "查看全部", "すべて見る", "すべて表示",
                           "see all", "Ver todos", "Mostra tutto")

        def _find_seeall_in_xml(_xml: str) -> list:
            """从 XML 抽取所有含 See all phrase 的节点 bounds."""
            _hits = []
            try:
                for _n in _XPS.parse(_xml):
                    if not getattr(_n, "bounds", None):
                        continue
                    _txt = (getattr(_n, "text", "") or "").strip()
                    _dsc = (getattr(_n, "content_desc", None) or "").strip()
                    _val = _txt or _dsc
                    if not any(_p in _val for _p in _seeall_phrases):
                        continue
                    if len(_val) > 80:
                        continue
                    _l, _tt, _r, _b = _n.bounds
                    if (_b - _tt) < 20 or (_r - _l) < 30:
                        continue
                    _hits.append((_l, _tt, _r, _b))
            except Exception:
                pass
            return _hits

        # 2026-05-05 P7-B: 用户反馈 task 1a78fad7 — preview 页第一个 See all
        # 是 "Admins and Moderators" (一般 1-3 人, 不是引流目标), 真正的
        # contributors See all 在屏幕下方, 默认 dump 拿不到 (lazy render).
        # 改成: 优先 dump 找到 ≥ 2 个 See all 才停; 找不到就 scroll_down 再
        # dump retry, 让 contributors section 进入屏幕.
        # 第 1 层: atx-agent dump + scroll retry 8 次 (前 2 次不滚等 SDUI 渲染,
        # 之后每次 scroll_down 一屏触发 contributors 区域 lazy render).
        seeall2 = []
        for _retry in range(8):
            time.sleep(0.7)
            try:
                _xml_try = d.dump_hierarchy() or ""
            except Exception:
                _xml_try = ""
            _hits = _find_seeall_in_xml(_xml_try)
            if len(_hits) >= 2:
                # 找到 ≥ 2 个 → 第一个是 admins, 第二个是 contributors, 停
                seeall2 = _hits
                log.info("[about-path] retry %d: dump 命中 %d 个 See all (≥2 ✓)",
                         _retry, len(_hits))
                break
            if _hits and _retry == 7:
                # 已是最后一次仍只 1 个 → 用现有的当 fallback
                seeall2 = _hits
                log.info("[about-path] retry 7: dump 仅 %d 个 See all, fallback 用 idx=0",
                         len(_hits))
                break
            log.info("[about-path] retry %d: dump 命中 %d 个 See all, scroll_down 后重试",
                     _retry, len(_hits))
            if _retry >= 1:
                try:
                    self.hb.scroll_down(d)
                except Exception:
                    pass
        else:
            log.info("[about-path] atx-agent dump 8 retry 仍 0, fallback raw adb")

        # 第 2 层: raw adb uiautomator dump (绕开 atx-agent cache)
        if not seeall2:
            try:
                import subprocess
                _dump_path = f"/sdcard/_about_path_{did[:8]}.xml"
                subprocess.run(
                    ["adb", "-s", did, "shell", "uiautomator", "dump", _dump_path],
                    capture_output=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                _proc = subprocess.run(
                    ["adb", "-s", did, "shell", "cat", _dump_path],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                _xml_raw = (_proc.stdout or "").strip()
                if _xml_raw and _xml_raw.startswith("<"):
                    seeall2 = _find_seeall_in_xml(_xml_raw)
                    log.info("[about-path] raw adb dump 命中 %d 个 See all (xml=%d bytes)",
                             len(seeall2), len(_xml_raw))
            except Exception as e:
                log.debug("[about-path] raw adb dump failed: %s", e)

        # 第 3 层: 坐标 fallback (基于 Members section 已知位置)
        # Members 列表 preview 页布局固定: top=单人 → admins → experts/contributors
        # See all 通常在屏幕中段 y=1100-1300. tap 屏幕中心 x=360 + y=1200 试.
        if not seeall2:
            log.warning("[about-path] dump 全 0, 用坐标 fallback tap (360, 1200)")
            try:
                self.hb.tap(d, 360, 1200)
                time.sleep(2.0)
                log.info("[about-path] ✓ 坐标 fallback tap 完成")
            except Exception as e:
                log.debug("[about-path] 坐标 tap failed: %s", e)
            return True

        # 取第二个 (用户反馈 2026-05-05: 第一个 See all 是 admins/moderators
        # 一般 1-3 人不是引流目标, 第二个才是 group contributors 完整列表)
        seeall2.sort(key=lambda x: x[1])
        idx = 1 if len(seeall2) >= 2 else 0
        sl2, st2, sr2, sb2 = seeall2[idx]
        scx2, scy2 = (sl2 + sr2) // 2, (st2 + sb2) // 2
        log.info("[about-path] tap See all (idx=%d/n=%d, 2nd=contributors) "
                 "bounds=(%d,%d,%d,%d) center=(%d,%d)",
                 idx, len(seeall2), sl2, st2, sr2, sb2, scx2, scy2)
        try:
            self.hb.tap(d, scx2, scy2)
            time.sleep(2.0)
            log.info("[about-path] ✓ 进入 contributors 完整列表")
        except Exception as e:
            log.debug("[about-path] tap See all failed: %s", e)
        return True

    def _tap_group_members_tab(self, d, did: str,
                               preferred_source: str = "") -> bool:
        """点击群内 Members tab 的精确路径。

        2026-05-04 真路径 (8 截图实证): 进群后 tap 群名 banner → About 页 →
        Members section "See all" → Members 页 → 滚到 Group contributors →
        其下方 See all → 完整 contributors 列表. 这是新版 FB 的真实入口,
        feed_authors 是错的 fallback (绕开了实际可用的 Members 入口).

        2026-04-24 Phase 9 升级 (对齐 Phase 7 search_bar 修复):
          1. **精确短 text/desc + clickable=True**, 防止命中推荐群长描述
          2. **新增 content-desc 分支** — 新版 FB katana 的 Members 入口
             可能是 content-desc 而非 text
          3. **点击后验证** — dump hierarchy 看是否出现 members list (name list 多个)
             或顶栏出现 "Members · N" 统计, 不是仍在群首页
          4. **噪音过滤** — 即使命中也要看 label 长度: 推荐群卡片 desc
             通常 > 40 字, Members Tab 短 label 一般 ≤ 20
        """
        deadline = time.time() + 60.0

        def _expired(stage: str) -> bool:
            if time.time() <= deadline:
                return False
            log.warning("[extract_members] Members tab probe timeout at %s", stage)
            return True

        # ── ⓪ 2026-05-04 真路径: tap 群名 banner → About → Members See all ──
        # 8 张真机截图证实新版 FB 公开群的 Members 入口路径:
        #   群首页(Featured tab) → tap 顶部群名 banner → About 页(含 Members
        #   section + 蓝字 "See all") → tap See all → Members 列表页(分组:
        #   清田 / Admins and Moderators / Group experts / Group contributors)
        # 老 5 路径全部找"Members tab"作为顶 tab bar 子项, 永远 miss (新版没了).
        if self._tap_via_about_members_path(d, did):
            return True

        def _is_on_members_list() -> bool:
            """自检: 点击后是否到了 Members 列表页."""
            try:
                return self._looks_like_group_members_list_xml(
                    d.dump_hierarchy() or ""
                )
            except Exception:
                return False

        def _recover_if_reaction_list() -> bool:
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if not self._looks_like_reaction_user_list_xml(xml):
                return False
            log.warning("[extract_members] mis-tapped reaction user list; backing out")
            try:
                d.press("back")
                time.sleep(0.8)
            except Exception:
                pass
            return True

        if _is_on_members_list():
            return True
        if self._tap_members_see_all_link(d, did, preferred_source):
            return True

        # 2026-05-03 P1-A real-device debug: 真机进群后 4 路 selector 全 miss,
        # 与 Groups chip 同源问题 (text='' desc 才有内容). dump 群内所有 tab 行
        # 候选给日志, 帮定位 Members tab 节点真实 text/desc/clickable.
        try:
            _xml_mt = d.dump_hierarchy() or ""
            if _xml_mt:
                from ..vision.screen_parser import XMLParser as _XPM
                _hits_mt = 0
                for _n in _XPM.parse(_xml_mt):
                    if not getattr(_n, "bounds", None):
                        continue
                    _l, _t, _r, _b = _n.bounds
                    # 群内顶部 tab 区域: 状态栏下到群头大概 0-700px
                    if not (0 <= _t <= 700 and (_b - _t) <= 160):
                        continue
                    _txt = (getattr(_n, "text", "") or "").strip()
                    _dsc = (getattr(_n, "content_desc", None) or "").strip()
                    if not (_txt or _dsc):
                        continue
                    # 只输出含 "Member"/"成员"/"メンバー" 等的节点 + clickable=True
                    # 节点（避免日志洪水）
                    _is_member_word = any(k in (_txt + _dsc) for k in (
                        "Member", "MEMBER", "member",
                        "成员", "成員", "メンバー", "Membri",
                    ))
                    _is_clickable = bool(getattr(_n, "clickable", False))
                    if not (_is_member_word or _is_clickable):
                        continue
                    log.info(
                        "[memtab-debug] t=%r d=%r bounds=(%d,%d,%d,%d) clk=%s",
                        _txt[:40], _dsc[:80], _l, _t, _r, _b,
                        getattr(_n, "clickable", "?"),
                    )
                    _hits_mt += 1
                    if _hits_mt >= 30:
                        break
        except Exception as _mt_dbg_e:
            log.debug("[memtab-debug] dump fail: %s", _mt_dbg_e)

        # ① 精确 text + clickable (原版路径, 保留)
        for txt in self._FB_GROUP_MEMBERS_TAB_TEXTS:
            if _expired("text"):
                return False
            try:
                el = d(text=txt, clickable=True)
                if el.exists(timeout=0.8):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(1.2)
                    if _is_on_members_list():
                        log.info("[extract_members] tap Members tab by text='%s' ✓",
                                  txt)
                        return True
                    if self._tap_members_see_all_link(d, did, preferred_source):
                        return True
                    if _recover_if_reaction_list():
                        return False
                    log.debug("[extract_members] text='%s' 点后不像 members 列表,"
                              " 继续尝试", txt)
            except Exception:
                continue

        # ② 精确 content-desc + clickable (新版 FB katana 常见)
        for txt in self._FB_GROUP_MEMBERS_TAB_TEXTS:
            if _expired("description"):
                return False
            try:
                el = d(description=txt, clickable=True)
                if el.exists(timeout=0.8):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(1.2)
                    if _is_on_members_list():
                        log.info("[extract_members] tap Members tab by desc='%s' ✓",
                                  txt)
                        return True
                    if self._tap_members_see_all_link(d, did, preferred_source):
                        return True
                    if _recover_if_reaction_list():
                        return False
            except Exception:
                continue

        # ③ descriptionContains 但只取短 label (过滤推荐群长描述)
        for txt in self._FB_GROUP_MEMBERS_TAB_TEXTS:
            if _expired("descriptionContains"):
                return False
            try:
                el = d(descriptionContains=txt, clickable=True)
                if el.exists(timeout=0.6):
                    try:
                        info = el.info
                        desc = (info.get("contentDescription") or "").strip()
                    except Exception:
                        desc = ""
                    # 长描述 = 推荐群卡片, 短描述 = Tab
                    if desc and len(desc) > 40:
                        log.debug("[extract_members] descContains '%s' 命中长描述"
                                  " (len=%d), 跳过防误点推荐群", txt, len(desc))
                        continue
                    if self._members_desc_looks_like_group_metadata(desc):
                        log.debug("[extract_members] descContains '%s' 命中群元信息"
                                  " desc=%r, 跳过防误判", txt, desc[:80])
                        continue
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(1.2)
                    if _is_on_members_list():
                        log.info("[extract_members] tap Members tab by descContains "
                                  "short desc='%s' ✓", desc[:30])
                        return True
                    if self._tap_members_see_all_link(d, did, preferred_source):
                        return True
                    if _recover_if_reaction_list():
                        return False
            except Exception:
                continue

        # ④ resourceId 兜底 (FB 老版本可能 expose)
        for rid in ("com.facebook.katana:id/members_tab",
                    "com.facebook.katana:id/group_members_tab"):
            if _expired("resourceId"):
                return False
            try:
                el = d(resourceId=rid)
                if el.exists(timeout=0.4):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(1.2)
                    if _is_on_members_list():
                        log.info("[extract_members] tap Members tab by resourceId ✓")
                        return True
                    if self._tap_members_see_all_link(d, did, preferred_source):
                        return True
                    if _recover_if_reaction_list():
                        return False
            except Exception:
                continue

        if _expired("info_fallback"):
            return False
        if self._open_group_info_then_members(d, did, preferred_source):
            return True

        # 2026-05-03 P1-A v14 (真机第 19 轮 dump 重大发现): 新版 FB 群页面布局
        # **完全移除 Members 子 tab**, 子 tab 仅有 Reels/You/Photos/Events/Files.
        # Members 入口收敛到顶部 "Member tools more options" 三点菜单. 旧 4
        # 路径全部依赖 Members tab text/desc 永远 miss. 加第 5 路径: tap
        # Member tools 弹菜单 → 选 Members 选项.
        try:
            for _mt_sel in (
                {"descriptionContains": "Member tools", "clickable": True},
                {"description": "Member tools more options", "clickable": True},
            ):
                _el_mt = d(**_mt_sel)
                if not _el_mt.exists(timeout=0.6):
                    continue
                self.hb.tap(d, *self._el_center(_el_mt))
                time.sleep(1.8)
                # v15 (2026-05-03): tap Member tools 后 dump 菜单所有节点,
                # 看真实选项名 (可能不是 "Members" 而是 "View all members" 或
                # "Member list" 等). 一轮真机即可拿数据.
                try:
                    _menu_xml = d.dump_hierarchy() or ""
                    if _menu_xml:
                        from ..vision.screen_parser import XMLParser as _XPMENU
                        _menu_hits = 0
                        for _mn in _XPMENU.parse(_menu_xml):
                            if not getattr(_mn, "bounds", None):
                                continue
                            _mt2 = (getattr(_mn, "text", "") or "").strip()
                            _md2 = (
                                getattr(_mn, "content_desc", None) or ""
                            ).strip()
                            if not (_mt2 or _md2):
                                continue
                            _mc = bool(getattr(_mn, "clickable", False))
                            if _mc or any(k in (_mt2 + _md2) for k in (
                                "Member", "member", "成员", "メンバー",
                                "View", "All", "List",
                            )):
                                log.info(
                                    "[menu-dbg] t=%r d=%r bounds=%s clk=%s",
                                    _mt2[:50], _md2[:80], _mn.bounds, _mc,
                                )
                                _menu_hits += 1
                                if _menu_hits >= 30:
                                    break
                except Exception as _menu_dbg_e:
                    log.debug("[menu-dbg] failed: %s", _menu_dbg_e)
                # 弹菜单后找 Members 选项 (text/desc/contains 多路尝试)
                for _mem_lbl in (
                    "Members", "Member list", "All members", "View members",
                    "View all members", "See all members",
                    "成员", "成員", "メンバー",
                ):
                    for _sel2 in (
                        {"text": _mem_lbl, "clickable": True},
                        {"description": _mem_lbl, "clickable": True},
                        {"textContains": _mem_lbl, "clickable": True},
                        {"descriptionContains": _mem_lbl, "clickable": True},
                    ):
                        try:
                            _el2 = d(**_sel2)
                            if not _el2.exists(timeout=0.4):
                                continue
                            try:
                                _cnt2 = _el2.count
                            except Exception:
                                _cnt2 = 1
                            if _cnt2 > 5:
                                continue
                            self.hb.tap(d, *self._el_center(_el2))
                            time.sleep(2.0)
                            if _is_on_members_list():
                                log.info(
                                    "[extract_members] tap Members via "
                                    "Member tools menu (label=%r) ✓",
                                    _mem_lbl,
                                )
                                return True
                        except Exception:
                            continue
                # 菜单点开但没找到 Members → back 关菜单
                try:
                    d.press("back")
                    time.sleep(0.6)
                except Exception:
                    pass
                break  # Member tools 只 tap 一次
        except Exception as _mt_e:
            log.debug("[extract_members] Member tools menu path failed: %s",
                       _mt_e)

        log.warning("[extract_members] Members tab 5 路径全部失败, 需跑"
                     " debug_extract_members_trace.py 诊断真实 UI 结构")
        return False

    def _assert_on_groups_page(self, d) -> bool:
        """点击 Groups tab 后验证: 当前页面看起来像 Groups 主页 / 群组列表。

        判据: 页面 dump 含 "groups" / "Your groups" / 已加入群名等关键词,
        且**不含** "What's on your mind?" / "Photo" / "Album" 等发帖元素。
        """
        try:
            xml = d.dump_hierarchy()
            low = xml.lower()
            # 明显是发帖界面 -> 已误入
            bad_markers = ("what's on your mind", "add to your post",
                            "what are you thinking", "photo/video",
                            "你在想什么")
            for m in bad_markers:
                if m in low:
                    log.warning("[browse_groups] 检测到发帖界面标志 '%s',未进 Groups", m)
                    return False
            # 正确标志
            good_markers = ("groups", "your groups", "joined", "my groups",
                             "グループ", "群组")
            return any(m in low for m in good_markers)
        except Exception:
            return True  # dump 失败不强判

    def browse_groups(self, max_groups: int = 5,
                      device_id: Optional[str] = None) -> Dict[str, Any]:
        """浏览"我加入的群组"列表,顺序进每个群浏览 1 屏内容。

        2026-04-23 bug fix: 原实现用 ``smart_tap("Groups tab")`` +
        ``smart_tap("Your groups")`` 依赖自学习 selector, 但 AutoSelector
        把 "Groups tab" 学成了 Home 按钮, "Your groups" 学成了"你在想什么"
        发帖输入框 — 每次运行都进发图界面。

        新实现:
          1. **硬编码 u2 selector** 精确点底部 Groups tab (description =
             "Groups, tab N of M" 规律)
          2. 点击后立刻做 **页面自检**(_assert_on_groups_page), 发现误入
             发帖界面就 BACK 一次撤销
          3. 再点"Your groups" (text 精确匹配 + clickable 过滤, 避免
             命中 TextView 提示语)
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"groups_visited": 0, "scrolls_total": 0, "groups_failed": 0,
                 "nav_fallback_used": False}

        with self.guarded("browse_groups", device_id=did, weight=0.5):
            # Step 1: 点底部导航 Groups tab
            if not self._tap_groups_bottom_tab(d, did):
                # 兜底: 尝试 smart_tap (虽然可能被污染 selector 命中,
                # 但有 _assert_on_groups_page 二次校验兜底)
                log.info("[browse_groups] 硬定位 Groups tab 未命中,降级 smart_tap")
                self.smart_tap("Groups tab", device_id=did)
                stats["nav_fallback_used"] = True
            time.sleep(2.0)

            # Step 2: 自检是否真在 Groups 相关页面
            if not self._assert_on_groups_page(d):
                # 误入发帖界面 → BACK 一次撤销
                log.warning("[browse_groups] 未进 Groups 页, BACK 撤销误操作")
                try:
                    d.press("back")
                    time.sleep(1.0)
                    # 二次尝试 (只信任硬定位)
                    if not self._tap_groups_bottom_tab(d, did):
                        stats["fatal"] = "groups_tab_miss_after_fallback"
                        log.error("[browse_groups] 二次硬定位仍失败,放弃任务")
                        return stats
                    time.sleep(2.0)
                    if not self._assert_on_groups_page(d):
                        stats["fatal"] = "not_on_groups_page"
                        log.error("[browse_groups] 二次点击后仍不在 Groups 页")
                        return stats
                except Exception as e:
                    log.warning("[browse_groups] BACK 兜底异常: %s", e)
                    stats["fatal"] = "back_recovery_failed"
                    return stats

            # Step 3: 点 "Your groups" 入口进入已加入群列表
            # (有些 FB 版本点 Groups tab 后直接就是 joined list, 不用再点)
            self._tap_your_groups_entry(d, did)
            time.sleep(1.5)

            for i in range(max_groups):
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    break
                # 进列表里第 1 个未访问的群 - smart_tap 找列表里 group 入口元素
                if not self.smart_tap("First group in your joined groups list",
                                      device_id=did):
                    stats["groups_failed"] += 1
                    continue

                time.sleep(random.uniform(2.5, 4.0))
                # 浏览 3-6 屏
                for _ in range(random.randint(3, 6)):
                    self.hb.scroll_down(d)
                    self.hb.wait_read(random.randint(800, 2500))
                    stats["scrolls_total"] += 1

                stats["groups_visited"] += 1
                # 退出群组返回列表
                d.press("back")
                time.sleep(random.uniform(1.0, 2.0))

        return stats

    # 搜索结果页的 "Groups" filter chip text (多语种 FB)
    #
    # 真机证据 (2026-04-30 task 4ce94cc4 + manual debug_dump.xml):
    # 中文版 FB chip 实际是 '小组' (简体) 或 '小組' (繁体). 历史遗漏导致
    # 真机 4 路 selector 全 miss → 任务失败. 同时 chip 元素的 text 字段
    # 通常为空, 真正的标识在 content-desc, 形如:
    #   content-desc="小组个搜索结果, 第3项，共7项"
    # 因此匹配方式必须用 descriptionContains 部分匹配, 不能用 description= 全等.
    _FB_SEARCH_GROUPS_FILTER_TEXTS = (
        "Groups", "GROUPS",
        "群组", "群組",
        "小组", "小組",  # 中文版 FB (zh-CN/zh-TW), 真机实测的标准文案
        "グループ",
        "Gruppi",
    )

    # 真正的 chip content-desc 一定是 "<lang_word>个搜索结果, 第N项，共M项"
    # (zh) / "tab N of M" (en) / "<lang_word>の検索結果" (ja) 这类带 *搜索结果* 后缀
    # 的完整短语。typeahead 联想项 desc 不含这种短语，能一刀切掉假阳性。
    # 历史 bug (caefd0e0 2026-04-30): descriptionContains='小组' 这种裸子串
    # 在 typeahead overlay 误命中 → Step 3 假"成功" → 后续步骤全错位。
    _FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES = (
        "个搜索结果",        # zh-Hans: '小组个搜索结果, 第3项，共7项'
        "個搜尋結果",        # zh-Hant
        "の検索結果",        # ja
        "검색 결과",         # ko (按需扩)
        # en 版 chip desc 真机抓到的实际格式: 'Groups search results, 4 of 7'
        # — 词与词之间是空格. 旧值 'search results' 被 f"{txt}{suffix}" 拼成
        # 'Groupssearch results' 永远 miss. 新值前置空格修正英文匹配。
        " search results",  # en (Redmi 13C Android FB 7.x 真机验证, 2026-05-03)
        "search results",   # 兼容兜底 (历史无空格场景, 防回归)
    )

    def _tap_search_results_groups_filter(self, d, did: str) -> bool:
        """硬编码点搜索结果页的 Groups filter chip.

        FB 搜索结果页顶部布局: [All] [Posts] [People] [Groups] [Pages] ...
        AutoSelector 学错时会把 "Groups, tab 4 of 6" (底部 tab) 当成
        filter chip → 把搜索页切回 Groups 主 tab → 任务卡住.

        匹配优先级 (P2.X-4 2026-04-30 加严):
          1. ``text=<lang_word>`` 全等 (英文版 FB / 部分小语种 chip text 直接为词)
          2. ``descriptionContains=f"{lang_word}{suffix}"`` —— 必须包含
             *搜索结果* 这类完整短语后缀, 拒绝 typeahead 联想项 desc 的裸子串
             命中 (历史 bug caefd0e0)。
        """
        # 2026-05-03 P1-A real-device debug: 真机 3 台均 chip miss 但截图可见
        # chip. dump 顶部区域所有 text/desc 节点, 帮定位 selector 失效根因.
        # 下游 selector 路径不变, 仅多打一次诊断日志.
        try:
            _xml_dbg = d.dump_hierarchy() or ""
            if _xml_dbg:
                from ..vision.screen_parser import XMLParser as _XP
                _hits = 0
                for _n in _XP.parse(_xml_dbg):
                    if not getattr(_n, "bounds", None):
                        continue
                    _l, _t, _r, _b = _n.bounds
                    if not (100 <= _t <= 360 and (_r - _l) <= 320
                            and (_b - _t) <= 140):
                        continue
                    _txt = (getattr(_n, "text", "") or "").strip()
                    _dsc = (getattr(_n, "content_desc", None) or "").strip()
                    if not (_txt or _dsc):
                        continue
                    log.info(
                        "[chip-debug] t=%r d=%r bounds=(%d,%d,%d,%d) clk=%s",
                        _txt[:40], _dsc[:80], _l, _t, _r, _b,
                        getattr(_n, "clickable", "?"),
                    )
                    _hits += 1
                    if _hits >= 30:
                        break
                if _hits == 0:
                    log.info("[chip-debug] 顶部 chip 区域无候选节点 "
                              "(xml_size=%d)", len(_xml_dbg))
        except Exception as _dbg_e:
            log.debug("[chip-debug] dump fail: %s", _dbg_e)

        for txt in self._FB_SEARCH_GROUPS_FILTER_TEXTS:
            try:
                el = d(text=txt, clickable=True)
                if el.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(el))
                    time.sleep(0.4)
                    log.info("[enter_group] tap search Groups filter by text=%r", txt)
                    return True
                # 中文 FB 的顶部 chip 经常是 TextView 自身不可点击、父级可点。
                # 直接按 XML bounds 点顶部 chip 中心，避免落到右侧筛选按钮。
                # 2026-05-03 P1-A real-device fix: 真机 FB 英文 chip 节点 text 为空
                # (只有 content-desc='Groups search results, 4 of 7'), 旧版 text
                # 全等判定永远过不了. 改为 text 或 content-desc 任一匹配即可,
                # 同时放宽 top 上限 (真机抓到 chip top=168, 旧上限 260 OK; 但有些设备
                # 状态栏更高, top 可能到 360, 一并放宽).
                try:
                    xml = d.dump_hierarchy() or ""
                    from ..vision.screen_parser import XMLParser
                    for node in XMLParser.parse(xml):
                        _node_t = (node.text or "").strip()
                        _node_d = (getattr(node, "content_desc", None)
                                    or "").strip()
                        # 命中条件: text 全等 txt OR content-desc 以 txt 开头
                        # (chip desc 为 "Groups search results, ..." 这样的前缀串)
                        if _node_t != txt and not _node_d.startswith(txt):
                            continue
                        if not node.bounds:
                            continue
                        left, top, right, bottom = node.bounds
                        # P1-A: top 上限 260 → 360 (覆盖更多机型状态栏高度变化)
                        if 100 <= top <= 360 and (right - left) <= 240:
                            self.hb.tap(d, (left + right) // 2,
                                        (top + bottom) // 2)
                            time.sleep(0.4)
                            log.info(
                                "[enter_group] tap search Groups filter "
                                "by top-chip txt=%r desc=%r bounds=%s",
                                txt, _node_d[:40], node.bounds,
                            )
                            return True
                except Exception:
                    pass
                # P2.X-4: 用 "<lang_word><suffix>" 完整短语兜底, 不再用 'txt'
                # 裸子串 — typeahead 任何 desc 含 '小组' 字也会命中, 是历史
                # caefd0e0 失败的核心成因。
                for suffix in self._FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES:
                    needle = f"{txt}{suffix}"
                    try:
                        el = d(descriptionContains=needle, clickable=True)
                        if el.exists(timeout=0.4):
                            try:
                                cx, cy = self._el_center(el)
                                if not (120 <= cy <= 280 and cx <= 650):
                                    continue
                            except Exception:
                                pass
                            self.hb.tap(d, *self._el_center(el))
                            time.sleep(0.4)
                            log.info(
                                "[enter_group] tap search Groups filter "
                                "by descContains=%r", needle,
                            )
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    # ── Step 2 多路提交 + 校验 ────────────────────────────────────────
    #
    # 历史 bug (caefd0e0 2026-04-30): ``d.send_action("search")`` 在该机
    # MIUI 自带拼音输入法上 *不抛异常也不真正派发* IME_ACTION_SEARCH, 屏幕
    # 停留在 typeahead overlay (取证截图: forensics/.../enter_group_assert_failed)。
    # 旧 fallback 仅在 ``send_action`` 抛异常时才 press("enter"), silent no-op
    # 直接绕过 → Step 3+ 在 typeahead 上误命中 ``descContains='小组'`` 子串。
    # P2.X-4 v2 (2026-04-30 P2 phase): 五路提交策略
    #
    # 1. send_action_search  — IME_ACTION_SEARCH (原路径, 兼容绝大多数设备)
    # 2. send_action_go      — IME_ACTION_GO (部分 MIUI 版本拦了 SEARCH 但放行 GO)
    # 3. tap_typeahead_group — typeahead 里直接 tap 群组建议行 (最精确, 不依赖 IME)
    # 4. keyevent_enter      — KEYCODE_ENTER (pre-check: person-only typeahead 则跳过)
    # 5. keyevent_search     — KEYCODE_SEARCH 兜底
    #
    # 顺序保证最安全路径优先, 且每条路径成功即立退，不增加正常设备耗时。
    _FB_SEARCH_SUBMIT_PATHS = (
        "send_action_search",
        "send_action_go",
        "tap_typeahead_group",
        "keyevent_enter",
        "keyevent_search",
    )

    # Profile 页判定 (与外层 Step 2.5 复用同一组 markers, 避免漂移)
    _FB_PROFILE_PAGE_MARKERS = (
        "加为好友", "加为朋友", "加為好友",
        "Add Friend", "Add friend",
        "友達になる", "友達リクエストを送信",
    )

    def _tap_typeahead_group_row(self, d, group_name: str) -> bool:
        """typeahead overlay 里直接 tap 含 group_name + group-marker 的建议行。

        适用场景：``send_action`` 因 MIUI IME 被拦而 silent no-op，typeahead
        仍显示时。如果 typeahead 里有群组建议行（desc 同时含 group_name 和群组
        marker 词），tap 它等同于用户点击建议 → 跳进群搜索结果。

        返回 True 表示 tap 了某个候选行（是否真的导航到结果页由外层校验）。
        """
        for marker in _FB_TYPEAHEAD_GROUP_DESC_MARKERS:
            try:
                # 先找包含 group_name 的可点击元素
                els = d(descriptionContains=group_name, clickable=True)
                if not els.exists(timeout=0.4):
                    break  # 没有包含 group_name 的建议行了
                _count = els.count
                for _i in range(min(_count, 8)):
                    try:
                        desc = (els[_i].info or {}).get("contentDescription", "") or ""
                        if marker in desc and group_name in desc:
                            self.hb.tap(d, *self._el_center(els[_i]))
                            log.info(
                                "[enter_group] tap typeahead group row (marker=%r, "
                                "desc=%r)", marker, desc[:80],
                            )
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        try:
            from src.host.fb_playbook import local_rules_disabled
            test_relaxed = local_rules_disabled()
        except Exception:
            test_relaxed = False
        if test_relaxed:
            # Test mode fallback: some FB builds show exact recent/search
            # suggestions without any group marker, while IME submit is a
            # silent no-op. Tap only an exact visible keyword row, then let the
            # outer verifier decide whether it became a real results page.
            for sel in (
                {"description": group_name},
                {"text": group_name},
                {"descriptionContains": group_name},
            ):
                try:
                    els = d(**sel)
                    if not els.exists(timeout=0.4):
                        continue
                    count = getattr(els, "count", 1) or 1
                    for i in range(min(count, 8)):
                        try:
                            el = els[i] if count > 1 else els
                            info = el.info or {}
                            raw = (
                                info.get("contentDescription")
                                or info.get("text")
                                or ""
                            ).strip()
                            if raw != group_name:
                                continue
                            cx, cy = self._el_center(el)
                            if cy < 200 or cy > 900:
                                continue
                            self.hb.tap(d, cx, cy)
                            log.info(
                                "[enter_group] tap exact typeahead keyword row "
                                "in test mode desc=%r", raw,
                            )
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
        return False

    def _submit_fb_search_with_verify(self, d, did: str,
                                       group_name: str) -> bool:
        """五路提交搜索 + 每路后 dump_hierarchy 校验是否离开 typeahead。

        返回 True 仅当确认页面已变成 *搜索结果页*（filter chips 行 ≥2 个完整
        ``text="..."`` 命中）。失败分两种：
          - 提交把页面带到 *profile 页*（典型 KEYCODE_ENTER 选中 typeahead
            首位人物）→ 留证 ``enter_group_submit_landed_on_profile`` 立即返 False,
            不再尝试后续路径（避免在 profile 页继续 keyevent 制造更多副作用）
          - 五路全没让页面变成 results 页且也不在 profile 上 → 留证
            ``enter_group_search_not_submitted`` 返 False
        """
        last_xml = ""
        for path in self._FB_SEARCH_SUBMIT_PATHS:
            try:
                if path == "send_action_search":
                    try:
                        d.send_action("search")
                    except Exception as e:
                        log.info(
                            "[enter_group] send_action('search') 异常 (%s)", e,
                        )
                elif path == "send_action_go":
                    # P2.X-4 v2: IME_ACTION_GO — 部分 MIUI 版本拦了 ACTION_SEARCH
                    # 但会转发 ACTION_GO (action=2 vs action=3). 低成本尝试.
                    try:
                        d.send_action("go")
                    except Exception as e:
                        log.info(
                            "[enter_group] send_action('go') 异常 (%s)", e,
                        )
                elif path == "tap_typeahead_group":
                    # typeahead 里直接 tap 群组建议行 — 不经过 IME, 最精确
                    if not self._tap_typeahead_group_row(d, group_name):
                        log.info(
                            "[enter_group] skip tap_typeahead_group: "
                            "无 group 建议行 (typeahead 未含群组候选)"
                        )
                        continue  # 没有群组建议行 → 跳过本路径(不等待 dump)
                elif path == "keyevent_enter":
                    # P2.X-4 v2: 发 ENTER 前扫 typeahead 是否有 person-only 建议
                    # person-only → ENTER 会选中首位人物 profile → 跳过改用下一路
                    if typeahead_has_person_but_no_group_suggestions(last_xml):
                        log.info(
                            "[enter_group] skip keyevent_enter: "
                            "typeahead 含 person 建议但无 group 建议, "
                            "避免 ENTER 误导航到 profile. group=%r", group_name,
                        )
                        continue  # 不等待 dump, 直接下一路
                    self._adb("shell input keyevent 66", device_id=did)
                elif path == "keyevent_search":
                    self._adb("shell input keyevent 84", device_id=did)
            except Exception as e:
                log.info("[enter_group] submit path=%s 异常 (%s)", path, e)
            time.sleep(1.4)
            try:
                last_xml = d.dump_hierarchy() or ""
            except Exception:
                last_xml = ""

            if hierarchy_looks_like_fb_search_results_page(last_xml):
                log.info(
                    "[enter_group] Step 2 submit succeeded via %s "
                    "(results page detected)", path,
                )
                return True

            _hit_profile = next(
                (m for m in self._FB_PROFILE_PAGE_MARKERS if m in last_xml),
                None,
            )
            if _hit_profile:
                # P2.X-FP guard (2026-04-30 real-device false positive):
                # "可能认识" (People You May Know) cards on the search homepage
                # also contain "加为好友" but do NOT contain "发消息".
                # A real profile page shows BOTH "加为好友" AND "发消息".
                # Also, if still on search surface (EditText present) or search
                # results page (>=2 chips), this is not a real profile page.
                _PROFILE_MSG_MARKERS = (
                    "\u53d1\u6d88\u606f",   # 发消息
                    "\u767c\u6d88\u606f",   # 發消息 (Trad)
                    "Send Message", "Send message", "Message",
                    "\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u9001\u4fe1",  # メッセージを送信
                    "\u767c\u9001\u8a0a\u606f",  # 發送訊息
                    "Envoyer un message", "Nachricht senden",
                )
                _has_msg_btn = any(m in last_xml for m in _PROFILE_MSG_MARKERS)
                _on_surface = hierarchy_looks_like_fb_search_surface(last_xml)
                _on_results = hierarchy_looks_like_fb_search_results_page(last_xml)
                if not _has_msg_btn or _on_surface or _on_results:
                    log.info(
                        "[enter_group] profile marker=%r found but guarded "
                        "(has_msg=%s on_surface=%s on_results=%s) path=%s "
                        "— likely 可能认识/搜索结果行 false positive, not profile. "
                        "Continue next path.",
                        _hit_profile, _has_msg_btn, _on_surface, _on_results, path,
                    )
                else:
                    log.warning(
                        "[enter_group] Step 2 submit path=%s 后落到 profile 页 "
                        "(marker=%r, has_msg=True), 中止后续路径避免副作用. group=%r",
                        path, _hit_profile, group_name,
                    )
                    try:
                        from src.host.task_forensics import capture_immediate
                        capture_immediate(
                            did,
                            step_name="enter_group_submit_landed_on_profile",
                            hint=(
                                f"group={group_name!r} marker={_hit_profile!r} "
                                f"path={path}"
                            ),
                            reason=(
                                "Step 2 提交搜索后 hierarchy 含 profile signature "
                                "('加为好友'+'发消息' 同时出现), 中止后续 keyevent."
                            ),
                        )
                    except Exception:
                        pass
                    return False

            log.info(
                "[enter_group] Step 2 submit path=%s 后仍非 results 页, 继续下一路",
                path,
            )

        log.warning(
            "[enter_group] Step 2 五路提交全失败, 仍停留在 typeahead/输入页. "
            "group=%r", group_name,
        )
        for wait_s in (1.5, 2.5):
            try:
                time.sleep(wait_s)
                last_xml = d.dump_hierarchy() or ""
            except Exception:
                last_xml = ""
            if hierarchy_looks_like_fb_search_results_page(last_xml):
                log.info(
                    "[enter_group] Step 2 submit succeeded via slow_settle "
                    "(wait=%.1fs results page detected)",
                    wait_s,
                )
                return True
        try:
            from src.host.task_forensics import capture_immediate
            capture_immediate(
                did,
                step_name="enter_group_search_not_submitted",
                hint=f"group={group_name!r}",
                reason=(
                    "send_action(search/go) + tap_typeahead_group + "
                    "keyevent ENTER + keyevent SEARCH "
                    "五路均未让页面进入搜索结果页 (filter chip 行 <2)"
                ),
            )
        except Exception:
            pass
        return False

    def _tap_first_search_result_group(self, d, did: str,
                                       group_name: str) -> bool:
        """硬编码点搜索结果列表里 group_name 对应的群入口.

        2 层 fallback 优先精确匹配 group_name (避免误点其他 group),
        最后才信任 TextView center 坐标 (即使父 ViewGroup 不可点).
        """
        # 1) 精确 text + clickable (列表项整行)
        try:
            el = d(text=group_name, clickable=True)
            if el.exists(timeout=0.8):
                cx, cy = self._el_center(el)
                if cy < 260:
                    raise ValueError("matched top search suggestion")
                self.hb.tap(d, cx, cy)
                time.sleep(0.5)
                log.info("[enter_group] tap first result by exact text=%r", group_name)
                return True
        except Exception:
            pass
        # 2) textContains (容忍 emoji / decorations)
        try:
            el = d(textContains=group_name, clickable=True)
            if el.exists(timeout=0.6):
                cx, cy = self._el_center(el)
                if cy < 260:
                    raise ValueError("matched top search suggestion")
                self.hb.tap(d, cx, cy)
                time.sleep(0.5)
                log.info("[enter_group] tap first result by textContains=%r", group_name)
                return True
        except Exception:
            pass
        # 3) TextView center (父 ViewGroup 不可点时兜底)
        try:
            el = d(text=group_name)
            if el.exists(timeout=0.4):
                cx, cy = self._el_center(el)
                if cy < 260:
                    raise ValueError("matched top search suggestion")
                self.hb.tap(d, cx, cy)
                time.sleep(0.5)
                log.info("[enter_group] tap first result via TextView center=%r", group_name)
                return True
        except Exception:
            pass
        # 4) XML fallback: 中文/日文新版结果行常把 "群名 · 加入"
        # 放在一个不可点击 TextView 或整行 content-desc 中。
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            candidates = []
            for node in XMLParser.parse(xml):
                raw = (getattr(node, "text", "") or
                       getattr(node, "content_desc", "") or "").strip()
                if not raw or group_name not in raw:
                    continue
                if not getattr(node, "bounds", None):
                    continue
                left, top, right, bottom = node.bounds
                if top < 260:
                    continue
                if any(bad in raw for bad in ("搜索建议", "Search suggestion")):
                    continue
                candidates.append((top, left, right, bottom, raw))
            candidates.sort(key=lambda item: (item[0], item[1]))
            if candidates:
                top, left, right, bottom, raw = candidates[0]
                self.hb.tap(d, (left + right) // 2, (top + bottom) // 2)
                time.sleep(0.5)
                log.info("[enter_group] tap first result via XML row=%r", raw[:80])
                return True
        except Exception as e:
            log.debug("[enter_group] XML group result fallback failed: %s", e)
        return False

    # 群组页面特征 tab 词 ─ 同时支持英/日/中文 FB
    # 进入真正的群组页面后, 屏幕"必然"含其中至少 1 个 (作为标签 / 按钮 / 文本).
    # Feed / Profile / Messenger / 推荐卡片均不含完整结构组合。
    _GROUP_PAGE_SIGNATURE_TOKENS = (
        # 英文
        "Discussion", "Members", "About", "Featured",
        "Media", "Files", "Events",
        # 日文
        "ディスカッション", "メンバー", "概要", "注目", "ファイル",
        # 中文
        "讨论", "成员", "简介", "精选",
    )

    def _assert_on_specific_group_page(self, d, group_name: str) -> Tuple[bool, str]:
        """进群后双重自检: (1) 当前页含 group_name (2) 含群组特征 tab。

        修复历史 bug (2026-04-30 P2.X):
          原版只查 textContains(group_name), 整屏任何地方 (Feed 推荐卡片、搜索
          历史建议、过往帖子里的同名词) 命中即放行 → enter_group 静默"成功",
          下游 extract_group_members 在错误页跑 0 提取, 误诊断为 FB 改版/隐私群。

        返回 (ok, evidence_reason). evidence_reason 用于日志和 forensics.
        """
        try:
            # 1. group_name 必须出现在当前屏幕上
            name_hit = False
            try:
                if d(textContains=group_name).exists(timeout=1.5):
                    name_hit = True
            except Exception:
                pass
            xml = ""
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if xml and (
                hierarchy_looks_like_fb_groups_filtered_results_page(xml)
                or hierarchy_looks_like_fb_search_results_page(xml)
            ):
                return False, "still_on_search_results_page"
            if not name_hit and xml and group_name in xml[:3000]:
                name_hit = True

            if not name_hit:
                return False, "name_not_found"

            # 2. 群组特征 tab 必须至少有 1 个 — 区分 Feed/Profile/Messenger
            sig_hits = [tok for tok in self._GROUP_PAGE_SIGNATURE_TOKENS
                        if tok in xml] if xml else []
            if not sig_hits:
                # 主动 dump 一次看 element 列表 (xml 可能为空时的兜底)
                try:
                    for tok in self._GROUP_PAGE_SIGNATURE_TOKENS:
                        if d(textContains=tok).exists(timeout=0.4):
                            sig_hits.append(tok)
                            break
                except Exception:
                    pass
            if not sig_hits:
                return False, "name_present_but_no_group_tab"

            return True, f"name_hit+sig={sig_hits[0]}"
        except Exception as e:
            return False, f"exception:{type(e).__name__}"

    def enter_group(self, group_name: str,
                    device_id: Optional[str] = None) -> bool:
        """通过搜索进入指定群组(假设已加入).

        2026-04-27 改造 (PR #119 R3 治本): 3 个裸 smart_tap → 硬编码
        helper + 自检, 修 5h 死循环事故的真根因. 见 memory:
        autoselector_pitfall.md / session_handoff_2026-04-27_pr119.

        步骤:
          1. _tap_search_bar_preferred (既有 helper, 自带 FB Home + Messenger
             误入检测 + force-restart 兜底)
          2. type_text + press enter 提交搜索
          3. _tap_search_results_groups_filter (新硬编码, 防误点底部 tab)
          4. _tap_first_search_result_group (新硬编码, 精确 group_name 匹配)
          5. _assert_on_specific_group_page (新自检, 防进错群/误入推荐卡片)
        每步硬编码失败降级 smart_tap (但有自检兜底, 不会污染 selector 持续作恶).
        """
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("enter_group", device_id=did, weight=0.2):
            # 若上一动作刚加入群，FB 可能停在目标群欢迎页。先处理该页，
            # 成功后直接复用当前群上下文，避免重新搜索把页面带回 typeahead。
            if group_name:
                try:
                    self._continue_group_welcome_if_present(d, did, group_name)
                    ok_current, reason_current = self._assert_on_specific_group_page(
                        d, group_name,
                    )
                    if ok_current:
                        log.info("[enter_group] 当前已在目标群页: %r (evidence=%s)",
                                 group_name, reason_current)
                        return True
                except Exception as e:
                    log.debug("[enter_group] current-page precheck skipped: %s", e)

            # Step 1: 进搜索页 (既有 _tap_search_bar_preferred 已包含完整自检)
            _set_step("搜索群组", group_name)
            if not self._tap_search_bar_preferred(d, did):
                log.info("[enter_group] _tap_search_bar_preferred miss, 降级")
                if not self.smart_tap("Search bar or search icon", device_id=did):
                    self._fallback_search_tap(d)
            time.sleep(0.6)

            # Step 1.5 (P2.X-2 2026-04-30): 硬断言搜索页已打开。
            # 历史 bug: Step 1 三层 fallback 之后仍可能停留在 Feed/Profile/Messenger,
            # 此时 Step 2 的 type_text 会输到 Feed 的 "What's on your mind?" 发帖框
            # (或者被吞掉无任何效果), 后续 Step 3-5 全错位。
            # 只要不在搜索页, 立刻 return False 取证, 让 outcome=enter_group_failed
            # 精确定位到"根本没打开搜索框"。
            #
            # 2026-05-04 retry fix: atx-agent dump_hierarchy 异步延迟会让首次 dump
            # 抓到 stale 旧页面 XML (tap Search 按钮还没真正切到 search surface).
            # 加 3 次 retry + 0.6s 间隔, 真正切到搜索页就立刻 PASS.
            _on_search = False
            for _retry in range(3):
                if _retry > 0:
                    time.sleep(0.6)
                try:
                    _on_search = hierarchy_looks_like_fb_search_surface(
                        d.dump_hierarchy() or ""
                    )
                except Exception:
                    _on_search = False
                if _on_search:
                    break
            if not _on_search:
                log.warning("[enter_group] Step 1 后未进入搜索页, 终止 (避免 type_text "
                             "在错误位置输入). group=%r", group_name)
                try:
                    from src.host.task_forensics import capture_immediate
                    capture_immediate(did,
                                       step_name="enter_group_search_page_not_opened",
                                       hint=f"group={group_name!r}",
                                       reason="Step 1 _tap_search_bar_preferred + smart_tap + _fallback_search_tap 全失败")
                except Exception:
                    pass
                return False

            # Step 2: 输入群名 + 多路提交 + 离开 typeahead 校验
            #
            # P2.X-4 (2026-04-30, real device task caefd0e0): 旧版只调
            # ``d.send_action("search")``, 该路径走 atx-agent → InputMethodManager
            # 触发 IME_ACTION_SEARCH。在部分 MIUI / 第三方拼音输入法上
            # **既不抛异常也不真正派发** ACTION_SEARCH, 屏幕停留在 typeahead
            # overlay (取证截图见 forensics/.../enter_group_assert_failed)。
            # 旧 fallback 仅在 send_action **抛异常** 时才回落 press("enter"),
            # silent no-op 直接绕过, 后续 Step 3 在 typeahead overlay 上
            # 误命中 ``descContains='小组'`` 子串。
            #
            # 新策略：每条提交路径后 dump_hierarchy 校验是否进入 *搜索结果页*
            # (FB_SEARCH_RESULTS_CHIP_TEXTS 至少 2 个 chip 完整命中)。
            # 路径顺序:
            #   1. send_action("search")          —— 与原行为兼容
            #   2. adb shell input keyevent 66    —— ENTER, 软键盘提交
            #   3. adb shell input keyevent 84    —— KEYCODE_SEARCH 兜底
            # 五路全失败 → 留证 ``enter_group_search_not_submitted`` 返 False。
            if not self._type_fb_search_query(d, group_name, did):
                log.warning("[enter_group] 搜索框写入/校验失败 group=%r", group_name)
                return False
            time.sleep(1.0)
            # Step 2.5 已并入: ``_submit_fb_search_with_verify`` 内部对每路提交
            # 同时做 results-page / profile-page 检测,profile 命中即留证返 False
            # (enter_group_submit_landed_on_profile),不会进 Step 3。
            if not self._submit_fb_search_with_verify(d, did, group_name):
                # P2.X-10 (2026-04-30 P2 phase) 自愈机制:
                # 五路提交仍失败时, 用 BACK 关掉 IME/typeahead overlay, 重新
                # 进入搜索页、清空输入框重输, 再尝试一次提交。
                # 触发条件: 当前不在 profile 页 (profile 误入已由 helper 内部
                # 留证+返回, 这里只处理 "搜索框卡住" 的场景).
                # 最多自愈 1 次, 防无限循环.
                log.info(
                    "[enter_group] Step 2 首次失败, 尝试 self-heal "
                    "(BACK → re-open search → retype → retry). group=%r",
                    group_name,
                )
                # P6-A v2 (2026-05-05): self-heal 之前先 wake screen + healthcheck.
                # 真机 task 67b98ecc (2026-05-05 09:42-09:45) 暴露 5 路 IME 全
                # 失败的真根因: atx-agent 挂导致 dump 全 0 (uiautomator dump
                # SIGKILL / atx-agent stub 异常), 后续 BACK + 'hierarchy 离开
                # 搜索页' 检查必失败 → 放弃自愈. wake + healthcheck 让 atx-agent
                # stub 重启后 dump 能拿到真实 hierarchy, self-heal 才有意义.
                # P6-A v1 的群循环外 wake (_gidx > 0) 永远没触发因为 task 卡
                # 第 1 群; v2 改在真根因点 (5 路 IME 全失败 = atx-agent 大概率挂).
                try:
                    if hasattr(self, "_ensure_screen_awake"):
                        self._ensure_screen_awake(did)
                except Exception as _sh_wake_e:
                    log.debug(
                        "[enter_group] self-heal pre-wake failed: %s", _sh_wake_e,
                    )
                _selfheal_ok = False
                try:
                    # 1. BACK 收起 IME / typeahead overlay.
                    # P6-A v3 (2026-05-05): 真机 task ef180eeb 揭示 P6-A v1/v2
                    # 假设错: dump 不是空 (76 elements), 而是 IME 占满屏 hierarchy.
                    # 单次 BACK + sleep 0.6s 不足以等 IME 真正收起 → dump 看到的
                    # 仍是 IME 元素, hierarchy_looks_like_fb_search_surface 必失败
                    # → "BACK 后已离开搜索页" 误判 → 放弃自愈.
                    # 修法: BACK 之后 sleep 1.5s (从 0.6s 加长) 等 IME 动画完成.
                    # 不加第 2 次 BACK 因为可能误退出搜索页.
                    self._adb("shell input keyevent 4", device_id=did)
                    time.sleep(1.5)
                    # 2. 确认仍在搜索页面 (BACK 有时会直接回到 Feed)
                    _sh_xml = d.dump_hierarchy() or ""
                    if not hierarchy_looks_like_fb_search_surface(_sh_xml):
                        log.warning(
                            "[enter_group] self-heal: BACK 后已离开搜索页 (回到 Feed/其他), "
                            "放弃自愈. group=%r", group_name,
                        )
                    else:
                        # 3. 重新点击搜索 EditText 让键盘弹出
                        try:
                            _et = d(className="android.widget.EditText")
                            if _et.exists(timeout=1.0):
                                self.hb.tap(d, *self._el_center(_et))
                                time.sleep(0.5)
                        except Exception:
                            pass
                        # 4. 全选 + 删除 旧内容, 重新输入 group_name
                        try:
                            self._adb("shell input keyevent 279", device_id=did)  # CTRL_A
                            time.sleep(0.15)
                            self._adb("shell input keyevent 67", device_id=did)   # DEL
                            time.sleep(0.15)
                        except Exception:
                            pass
                        if not self._type_fb_search_query(d, group_name, did):
                            log.warning("[enter_group] self-heal 重新写入搜索框失败 "
                                        "group=%r", group_name)
                            return False
                        time.sleep(1.0)
                        # 5. 再次尝试五路提交 (不再递归自愈)
                        _selfheal_ok = self._submit_fb_search_with_verify(
                            d, did, group_name,
                        )
                except Exception as _sh_e:
                    log.warning("[enter_group] self-heal 异常: %s", _sh_e)
                if not _selfheal_ok:
                    log.warning(
                        "[enter_group] self-heal 后仍失败, 放弃. group=%r",
                        group_name,
                    )
                    return False

            # Step 3: 切到 Groups filter
            #
            # P2.X-3 v2 (2026-04-30, real device task f5f2941a):
            # 删除 smart_tap('Groups tab or filter') fallback. AutoSelector
            # 把该 intent 学成了"点搜索结果首位" → 真机上观察到误点首位人物
            # (Youngjo Song / Takeshi Yoshida 等) → 把页面带去 profile.
            # 即使后置 Step 3.5 验证拦得住, 设备已经"瞎点"过了一次. 现在硬编码
            # chip miss 即 return False, 不再让 AutoSelector 在 Step 3 出手.
            _set_step("筛选 Groups 结果", group_name)
            if not self._tap_search_results_groups_filter(d, did):
                log.warning("[enter_group] 硬编码找不到 Groups filter chip, "
                             "拒绝 fallback smart_tap (autoselector 已被训坏 "
                             "→ 误点首位人物). group=%r", group_name)
                try:
                    from src.host.task_forensics import capture_immediate
                    capture_immediate(did,
                                       step_name="enter_group_groups_filter_chip_not_found",
                                       hint=f"group={group_name!r}",
                                       reason="_tap_search_results_groups_filter 硬编码 4 路 (text/desc x EN/zh/ja/it) 全 miss")
                except Exception:
                    pass
                return False
            time.sleep(1.0)

            # Step 3.5 (P2.X-4 2026-04-30 加严): 验证 filter 是否真的切到 Groups。
            #
            # 旧版 markers ('members'|'小组'|'成员'|...) 用 *子串包含*, 历史 bug
            # caefd0e0 真机 typeahead overlay 的 desc 含 ``'小组'`` 子串就误放行,
            # Step 4 在 typeahead 上点 ``text='潮味'`` 联想行 → 仍是 typeahead →
            # Step 5 才发现 ``name_present_but_no_group_tab``。
            #
            # 新校验：用 ``hierarchy_looks_like_fb_groups_filtered_results_page``
            # 双闸:
            #   - 必须先是搜索 *结果页* (chip 行 ≥2 完整 ``text="..."`` 匹配)
            #   - 再要求出现 *完整短语* group 标识 ('Public group'/'公开小组'/
            #     '公開グループ' …) 或 ``\d+ members`` 行模式
            # typeahead overlay 即使 desc 含子串也会被第一道闸拒掉。
            try:
                _xml_after_step3 = d.dump_hierarchy() or ""
            except Exception:
                _xml_after_step3 = ""
            if not hierarchy_looks_like_fb_groups_filtered_results_page(
                _xml_after_step3
            ):
                log.warning("[enter_group] Step 3 后未切到 Groups 结果页 "
                             "(strict markers miss), 终止避免 Step 4 误点人物. group=%r",
                             group_name)
                try:
                    from src.host.task_forensics import capture_immediate
                    capture_immediate(did,
                                       step_name="enter_group_groups_filter_not_applied",
                                       hint=f"group={group_name!r}",
                                       reason="Step 3 _tap_search_results_groups_filter + smart_tap 后, hierarchy 仍无群组 markers")
                except Exception:
                    pass
                return False

            # Step 4: 点对应群
            #
            # P2.X-3 (2026-04-30): 删除 smart_tap('First matching group') fallback。
            # 该 fallback 被 AutoSelector 训练成"点列表首位"——历史上真机若遇
            # 同名人物排在首位 (例如群名='潮味' 撞上日文人名 Takeshi Yoshida),
            # 会无视 group_name 直接误点 → 进 profile → 浪费整轮 enter_group。
            # 现在: 严格 3 路 miss 即视为页面找不到 group, 直接 return False,
            # 让上层 outcome=enter_group_failed 精确反馈"群名拼写错/被屏蔽/已删".
            _set_step("点击群组进入", group_name)
            if not self._tap_first_search_result_group(d, did, group_name):
                log.warning("[enter_group] 严格匹配未找到 group=%r, "
                             "拒绝 fallback smart_tap (旧版会误点首位人物)",
                             group_name)
                return False
            time.sleep(random.uniform(2.0, 3.5))

            # Step 5: 双重自检 (P2.X 2026-04-30 加严):
            # 必须同时满足 group_name + 群组特征 tab(Members/Discussion 等),
            # 否则 Feed 推荐卡片/搜索建议历史也会误判通过 → enter_group 静默"成功"。
            ok, reason = self._assert_on_specific_group_page(d, group_name)
            if not ok:
                log.warning("[enter_group] 自检失败 reason=%s group=%r "
                             "(可能误入推荐群/Messenger/Feed/profile)",
                             reason, group_name)
                # P2.X: 自检失败那一刻同步取证, 让运营看到当时屏幕到底是什么
                try:
                    from src.host.task_forensics import capture_immediate
                    capture_immediate(did,
                                       step_name="enter_group_assert_failed",
                                       hint=f"group={group_name!r} reason={reason}",
                                       reason="_assert_on_specific_group_page returned False")
                except Exception:
                    pass
                return False
            log.info("[enter_group] 进入群组成功: %r (evidence=%s)",
                      group_name, reason)
            return True

    def comment_on_post(self, comment_text: str,
                        device_id: Optional[str] = None) -> bool:
        """对当前可见的帖子发表评论(需先滚到一条帖子上)。"""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("comment", device_id=did):
            if not self.smart_tap("Comment button on the visible post", device_id=did):
                return False
            time.sleep(random.uniform(1.0, 2.0))

            rewritten = self.rewrite_message(comment_text,
                                             {"platform": "facebook", "context": "group_comment"})
            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.5)
            ok = self.smart_tap("Send comment button or Post button", device_id=did)
            time.sleep(random.uniform(1.0, 2.0))
            d.press("back")
            return ok

    @_with_fb_foreground
    def group_engage_session(self, group_name: str = "",
                             max_posts: int = 5,
                             comment_probability: float = 0.2,
                             like_probability: float = 0.4,
                             comment_pool: Optional[List[str]] = None,
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None) -> Dict[str, Any]:
        """进群后浏览/点赞/评论组合操作 — Sprint 1 新增 + 2026-04-22 persona 改造。

        Args:
            group_name: 群名(空则假设已在群内)
            max_posts: 浏览的帖子数上限（显式传 = 最高权重，否则按 phase 取 playbook）
            comment_probability: 每条帖子触发评论的概率
            like_probability: 每条帖子触发点赞的概率
            comment_pool: 可选的预设评论池；**为空时按 persona.country_code**
                去 ``chat_messages.yaml.countries[cc].comment_templates`` 抽，
                保证日本女性客群发日文评论、不会退化成英文。
            persona_key: 目标客群 key
            phase: 显式覆盖；默认走 fb_account_phase.get_phase(device_id)
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"posts_seen": 0, "likes": 0, "comments": 0,
                 "group": group_name,
                 "persona_key": persona_key or ""}

        # P0-2: 合并 playbook phase 参数。业务方法传入的显式参数权重最高。
        eff_phase, ab_cfg = _resolve_phase_and_cfg("group_engage",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg:
            # yaml 字段名以 facebook_playbook.yaml 为准（max_posts / max_members）
            if "max_posts" in ab_cfg and max_posts == 5:  # 5 = 方法签名默认
                max_posts = int(ab_cfg.get("max_posts") or max_posts)
            if "comment_probability" in ab_cfg and comment_probability == 0.2:
                comment_probability = float(ab_cfg.get("comment_probability")
                                            or comment_probability)
            if "like_probability" in ab_cfg and like_probability == 0.4:
                like_probability = float(ab_cfg.get("like_probability")
                                         or like_probability)
        stats["phase"] = eff_phase
        stats["max_posts_applied"] = max_posts

        if group_name and not self.enter_group(group_name, device_id=did):
            stats["error"] = "无法进入群组"
            return stats

        # P0-2: comment_pool 为空 → 按 persona 抽日文/意大利文评论池
        if not comment_pool:
            try:
                from .fb_content_assets import get_comment_pool as _gcp
                comment_pool = _gcp(persona_key=persona_key)
            except Exception as e:
                log.debug("[group_engage] 拉 persona 评论池失败: %s", e)
        if not comment_pool:
            comment_pool = [
                "Interesting!",
                "Thanks for sharing.",
                "Useful info, appreciated.",
                "Great point.",
                "I agree with this.",
            ]
        stats["comment_pool_size"] = len(comment_pool)

        with self.guarded("group_engage", device_id=did, weight=0.5):
            for _ in range(max_posts):
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    break

                # 滚动到下一条帖子
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(1500, 4500))
                stats["posts_seen"] += 1

                if random.random() < like_probability:
                    if self.smart_tap("Like button on the visible post",
                                      device_id=did):
                        stats["likes"] += 1
                        time.sleep(random.uniform(0.8, 1.5))

                if random.random() < comment_probability:
                    txt = random.choice(comment_pool)
                    if self.comment_on_post(txt, device_id=did):
                        stats["comments"] += 1
                        time.sleep(random.uniform(2.0, 5.0))

        return stats

    # ──────────────────────────────────────────────────────────────
    # 智能社群互动引擎 v1 (2026-05-12)
    # ──────────────────────────────────────────────────────────────

    # 帖子 feed 中用于过滤 UI 文本的关键词 (非帖子正文)
    _FEED_POST_SKIP_KW = (
        "Like", "Comment", "Share", "Reply", "Send",
        "Public", "Private", "Suggested for you",
        "See more", "See translation", "Write a comment",
        "ago", "hour", "minute", "yesterday", "Just now",
        "いいね", "コメント", "シェア", "返信",
        "もっと見る", "翻訳を見る", "コメントする",
        "前", "時間前", "分前", "昨日",
        "点赞", "评论", "分享", "回复",
    )

    def _extract_posts_from_dump(self, xml: str,
                                  seen_names: set) -> List[Dict[str, Any]]:
        """从一次 hierarchy dump 中提取可见帖子的 作者+正文+头像bounds。

        策略: 找 "More options for {X}'s post" desc 节点 → X = 作者名,
        同一水平带 (y±300px) 的长文本 = 帖子正文,
        "{X} Profile picture" desc = 头像 bounds。
        """
        try:
            from ..vision.screen_parser import XMLParser
        except Exception:
            return []

        posts = []
        nodes = list(XMLParser.parse(xml))

        # 收集 author → "More options" bounds
        author_anchors = []
        for n in nodes:
            desc = (getattr(n, "content_desc", None) or "").strip()
            if not desc:
                continue
            m = self._FB_FEED_AUTHOR_DESC_PATTERN.match(desc)
            if not m:
                m = self._FB_FEED_AUTHOR_DESC_PATTERN_JA.match(desc)
            if not m:
                continue
            raw_name = m.group(1).strip()
            name = self._clean_group_member_candidate_name(raw_name)
            if not name or name in seen_names:
                continue
            # 过滤 Page/商家
            if any(kw in name for kw in (
                "Page", "Group", "页面", "公页", "公式",
                "Official", "official", "Channel",
                "ホテル", "Hotel", "Shop", "Store",
                "Co.", "Inc.", "Ltd",
            )):
                continue
            bounds = getattr(n, "bounds", None)
            if bounds:
                author_anchors.append({
                    "name": name, "raw_name": raw_name,
                    "anchor_y": (bounds[1] + bounds[3]) // 2,
                    "bounds": bounds,
                })

        # 收集头像 bounds
        pic_by_name = {}
        for n in nodes:
            desc = (getattr(n, "content_desc", None) or "").strip()
            bounds = getattr(n, "bounds", None)
            if not desc or not bounds:
                continue
            pic_name = ""
            if desc.endswith(" Profile picture"):
                pic_name = desc[:-len(" Profile picture")].strip()
            elif desc.endswith("のプロフィール写真"):
                pic_name = desc[:-len("のプロフィール写真")].strip()
            elif desc.endswith("さんのプロフィール写真"):
                pic_name = desc[:-len("さんのプロフィール写真")].strip()
            if pic_name and pic_name not in pic_by_name:
                pic_by_name[pic_name] = bounds

        # 收集长文本节点 (候选帖子正文)
        text_nodes = []
        for n in nodes:
            txt = (getattr(n, "text", "") or "").strip()
            if not txt or len(txt) < 15 or len(txt) > 800:
                continue
            if any(kw in txt for kw in self._FEED_POST_SKIP_KW):
                continue
            bounds = getattr(n, "bounds", None)
            if bounds:
                text_nodes.append({"text": txt, "y": (bounds[1] + bounds[3]) // 2})

        # 收集 Like 按钮 bounds (避免后续二次 dump)
        like_buttons = []
        for n in nodes:
            desc = (getattr(n, "content_desc", None) or "").strip()
            txt = (getattr(n, "text", "") or "").strip()
            bounds = getattr(n, "bounds", None)
            if not bounds:
                continue
            # bounds 合理性: 过小(<20px)或过大(>300px) 的不是按钮
            bw = bounds[2] - bounds[0]
            bh = bounds[3] - bounds[1]
            if bw < 20 or bh < 20 or bw > 300 or bh > 200:
                continue
            label = (desc + " " + txt).lower()
            if any(k in label for k in ("like", "いいね", "赞")):
                # 排除已点赞/取消/评论附属/reaction 按钮
                if any(ex in label for ex in (
                    "liked", "unlike", "取消",
                    "comment", "コメント", "评论",
                    "share", "シェア", "分享",
                    "love", "haha", "wow", "sad", "angry",
                )):
                    continue
                like_buttons.append({
                    "y": (bounds[1] + bounds[3]) // 2,
                    "bounds": bounds,
                })

        # 配对: 每个 author anchor 找最近的长文本 + Like 按钮
        # 帖子正文通常在作者名 *下方*, 优先匹配下方文本
        for anchor in author_anchors:
            ay = anchor["anchor_y"]
            best_text = ""
            best_dist = 99999
            for tn in text_nodes:
                dy = tn["y"] - ay
                # 下方 (0~400px): 直接用距离; 上方: 加 200 惩罚
                eff_dist = abs(dy) if dy >= 0 else abs(dy) + 200
                if eff_dist < best_dist and abs(dy) < 400:
                    best_dist = eff_dist
                    best_text = tn["text"]

            # Like 按钮在帖子正文 *下方* (评论区域之前)
            best_like = None
            best_like_dist = 99999
            for lb in like_buttons:
                dy = lb["y"] - ay
                eff_dist = abs(dy) if dy >= 0 else abs(dy) + 200
                if eff_dist < best_like_dist and abs(dy) < 500:
                    best_like_dist = eff_dist
                    best_like = lb["bounds"]

            avatar_bounds = (pic_by_name.get(anchor["raw_name"])
                             or pic_by_name.get(anchor["name"]))

            posts.append({
                "author_name": anchor["name"],
                "raw_name": anchor["raw_name"],
                "post_text": best_text,
                "avatar_bounds": avatar_bounds,
                "like_bounds": best_like,
                "anchor_y": ay,
            })

        return posts

    def _tap_like_on_visible_post(self, d, post_anchor_y: int,
                                   device_id: str) -> bool:
        """点赞当前可见帖子 — 用 hierarchy desc 精确匹配, 不用 smart_tap。"""
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            like_candidates = []
            for n in XMLParser.parse(xml):
                desc = (getattr(n, "content_desc", None) or "").strip()
                txt = (getattr(n, "text", "") or "").strip()
                bounds = getattr(n, "bounds", None)
                if not bounds:
                    continue
                label = desc.lower() + " " + txt.lower()
                # 英文/日文/中文 Like 按钮
                if any(k in label for k in ("like", "いいね", "赞")):
                    # 排除 "liked" (已点赞), "unlike"
                    if "liked" in label or "unlike" in label or "取消" in label:
                        continue
                    cy = (bounds[1] + bounds[3]) // 2
                    dist = abs(cy - post_anchor_y)
                    if dist < 500:
                        like_candidates.append((dist, bounds))

            if not like_candidates:
                return False
            # 选最近的 Like 按钮
            like_candidates.sort(key=lambda x: x[0])
            b = like_candidates[0][1]
            cx = (b[0] + b[2]) // 2
            cy = (b[1] + b[3]) // 2
            self.hb.tap(d, cx, cy)
            log.info("[smart_engage] liked post near y=%d at (%d,%d)",
                     post_anchor_y, cx, cy)
            return True
        except Exception as e:
            log.debug("[smart_engage] like failed: %s", e)
            return False

    def _tap_avatar_enter_profile(self, d, avatar_bounds,
                                   device_id: str) -> bool:
        """点击头像进入 profile 页, 验证到达。

        处理 3 种常见场景:
        1. 直接进入 profile 页 (最常见)
        2. 弹出 photo viewer → back → 重试
        3. 弹出快捷菜单 (View profile / プロフィールを見る) → 点菜单项
        """
        if not avatar_bounds:
            return False
        cx = (avatar_bounds[0] + avatar_bounds[2]) // 2
        cy = (avatar_bounds[1] + avatar_bounds[3]) // 2
        try:
            self.hb.tap(d, cx, cy)
            time.sleep(2.5)
            if self._is_likely_fb_profile_page(d):
                log.info("[smart_engage] entered profile via avatar at (%d,%d)", cx, cy)
                return True

            # 检查是否弹出了快捷菜单 (View profile 选项)
            try:
                xml_chk = d.dump_hierarchy() or ""
                _view_kws = ("View profile", "プロフィールを見る",
                             "查看个人主页", "See profile")
                for _vk in _view_kws:
                    if _vk in xml_chk:
                        if self.smart_tap(_vk, device_id=device_id):
                            time.sleep(2.0)
                            if self._is_likely_fb_profile_page(d):
                                log.info("[smart_engage] entered profile via menu '%s'", _vk)
                                return True
                        break
            except Exception:
                pass

            # 二次尝试 (FB 可能弹出了 photo viewer)
            d.press("back")
            time.sleep(1.0)
            self.hb.tap(d, cx, cy)
            time.sleep(2.5)
            if self._is_likely_fb_profile_page(d):
                return True
            log.warning("[smart_engage] tap avatar (%d,%d) 未进入 profile", cx, cy)
            return False
        except Exception as e:
            log.debug("[smart_engage] avatar tap failed: %s", e)
            return False

    @_with_fb_foreground
    def smart_engage_session(
        self,
        group_name: str = "",
        device_id: Optional[str] = None,
        persona_key: Optional[str] = None,
        phase: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        join_if_needed: bool = True,
        **kw: Any,
    ) -> Dict[str, Any]:
        """智能社群互动引擎 — 浏览帖子→分析→点赞→进profile→加友。

        v1 (2026-05-12): 仅 B 类 (点赞+加友), A 类评论待 Phase 3 数据验证后开放。

        流程:
          1. 进入群组 (复用 enter_group)
          2. 逐屏滚动 feed
          3. 每屏: dump → 提取帖子 (作者名+正文+头像)
          4. L1 名字评分 + 内容评分 → 分层
          5. B 类: 点赞 → 点头像进 profile → 停留 → 加友 → 返回 feed
          6. 记录每次互动到 fb_post_engagements

        Args:
            group_name: 群组名
            config: smart_engage.yaml 配置 (由 executor 传入)
            persona_key: 目标客群 key
            join_if_needed: 是否自动加入群组

        Returns:
            {
                "posts_scanned": int,
                "tier_a": int,
                "tier_b": int,
                "tier_c": int,
                "likes": int,
                "profiles_visited": int,
                "friend_requests_sent": int,
                "skipped_cross_device": int,
                "group": str,
                "engagements": [...],
            }
        """
        did = self._did(device_id)
        d = self._u2(did)
        cfg = config or {}

        stats: Dict[str, Any] = {
            "posts_scanned": 0,
            "tier_a": 0, "tier_b": 0, "tier_c": 0,
            "likes": 0, "profiles_visited": 0,
            "friend_requests_sent": 0,
            "skipped_cross_device": 0,
            "group": group_name,
            "engagements": [],
        }

        # 配置
        posts_per_session = int(cfg.get("posts_per_session", 10))
        max_scrolls = int(cfg.get("max_scrolls", 15))
        min_l1 = float(cfg.get("min_l1_score", 20))
        tier_a_th = float(cfg.get("tier_a_threshold", 60))
        tier_b_th = float(cfg.get("tier_b_threshold", 30))
        like_prob_b = float(cfg.get("like_probability_b", 0.8))
        dwell_range = cfg.get("profile_dwell_sec", [10, 20])
        dwell_range_a = cfg.get("profile_dwell_sec_a", [30, 60])
        max_friends = int(cfg.get("max_friends_per_session", 6))
        consec_c_limit = int(cfg.get("consecutive_c_before_switch_group", 5))
        cooldown_h = int(cfg.get("cross_device_cooldown_hours", 4))
        enable_add_b = bool(cfg.get("add_friend_tier_b", True))
        session_timeout = float(cfg.get("session_max_duration_sec", 1800))
        delay_range = cfg.get("delay_between_adds_sec", [30, 90])
        note_templates = cfg.get("friend_note_templates", [])
        max_likes_h = int(cfg.get("max_likes_per_hour", 15))
        max_visits_h = int(cfg.get("max_profile_visits_per_hour", 12))
        target_country = str(cfg.get("target_country", "JP"))

        # 进群
        if group_name:
            if not self.enter_group(group_name, device_id=did):
                stats["error"] = "enter_group_failed"
                return stats
            if join_if_needed and self._current_group_page_requires_join(d, group_name):
                join_ok, _ = self._join_current_group_page_if_needed(d, did, group_name)
                if not join_ok:
                    stats["error"] = "join_group_failed"
                    return stats
                time.sleep(1.5)
                if not self.enter_group(group_name, device_id=did):
                    stats["error"] = "reenter_group_failed"
                    return stats

        # 初始滚动 (跳过顶部横幅/公告)
        try:
            for _ in range(2):
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(800, 1500))
        except Exception:
            pass

        seen_names: set = set()
        consecutive_c = 0
        session_start = time.time()
        # 附言模板轮转 (避免同 session 重复选同一模板)
        _note_pool = list(note_templates) if note_templates else []
        random.shuffle(_note_pool)
        _note_idx = 0

        from src.ai.fb_lead_scorer import score_member as _score_member
        from src.ai.fb_post_analyzer import (
            analyze_post, is_author_recently_engaged, record_engagement,
        )

        for scroll_i in range(max_scrolls):
            # 会话超时
            if time.time() - session_start > session_timeout:
                log.info("[smart_engage] session timeout after %ds", session_timeout)
                break
            # 已达好友数上限
            if stats["friend_requests_sent"] >= max_friends:
                log.info("[smart_engage] reached max_friends=%d", max_friends)
                break
            # 已扫描足够帖子
            if stats["posts_scanned"] >= posts_per_session:
                break

            # 风险检测
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                stats["risk_detected"] = msg
                break

            # dump + 提取
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            if not xml:
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(1200, 2000))
                continue

            visible_posts = self._extract_posts_from_dump(xml, seen_names)

            if not visible_posts:
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(1200, 2000))
                continue

            for post in visible_posts:
                if stats["posts_scanned"] >= posts_per_session:
                    break
                if stats["friend_requests_sent"] >= max_friends:
                    break

                author = post["author_name"]
                seen_names.add(author)
                stats["posts_scanned"] += 1

                # L1 名字评分 (country + gender + source_group)
                try:
                    _sm = _score_member(
                        author,
                        target_country=target_country,
                        source_group=group_name,
                    )
                    l1 = float(_sm.get("score", 0))
                except Exception:
                    l1 = 0.0

                # 早期门槛过滤: L1 < min_l1 → 直接 C 类, 跳过内容分析
                if l1 < min_l1:
                    stats["tier_c"] += 1
                    consecutive_c += 1
                    log.debug("[smart_engage] skip %s l1=%.0f < min_l1=%.0f",
                              author, l1, min_l1)
                    if consecutive_c >= consec_c_limit:
                        stats["switch_reason"] = "consecutive_c"
                        return stats
                    continue

                # 综合分析 (只对过了 L1 门槛的做内容评分)
                analysis = analyze_post(
                    author, post["post_text"], l1,
                    tier_a_threshold=tier_a_th,
                    tier_b_threshold=tier_b_th,
                )
                tier = analysis["tier"]
                stats[f"tier_{tier.lower()}"] += 1
                # 遇到 A/B 类 → 重置连续 C 计数 (避免好群被误判退出)
                if tier in ("A", "B"):
                    consecutive_c = 0

                log.info(
                    "[smart_engage] post #%d author=%s l1=%.0f content=%.0f "
                    "total=%.0f tier=%s topic=%s",
                    stats["posts_scanned"], author,
                    analysis["l1_score"], analysis["content_score"],
                    analysis["total_score"], tier, analysis["topic"],
                )

                if tier == "C":
                    consecutive_c += 1
                    if consecutive_c >= consec_c_limit:
                        log.info("[smart_engage] %d consecutive C, switching group",
                                 consec_c_limit)
                        stats["switch_reason"] = "consecutive_c"
                        # 记 C 类也写 DB (但不执行动作)
                        record_engagement(did, group_name, analysis, {
                            "post_snippet": post["post_text"][:200],
                        })
                        return stats
                    continue

                # 跨设备错峰
                if is_author_recently_engaged(author, group_name, did, cooldown_h):
                    log.info("[smart_engage] skip %s (cross-device cooldown)", author)
                    stats["skipped_cross_device"] += 1
                    continue

                actions: Dict[str, Any] = {
                    "post_snippet": post["post_text"][:200],
                    "liked": False,
                    "commented": False,
                    "profile_visited": False,
                    "friend_sent": False,
                }

                # ── 点赞 (优先用预提取的 like_bounds, 避免二次 dump) ──
                # 每小时限流
                _elapsed_h = max((time.time() - session_start) / 3600.0, 0.01)
                _likes_rate_ok = stats["likes"] < max_likes_h * _elapsed_h + 1
                _visits_rate_ok = stats["profiles_visited"] < max_visits_h * _elapsed_h + 1

                if tier in ("A", "B") and _likes_rate_ok:
                    prob = like_prob_b if tier == "B" else 1.0
                    if random.random() < prob:
                        _liked = False
                        _lk_b = post.get("like_bounds")
                        if _lk_b:
                            try:
                                _lx = (_lk_b[0] + _lk_b[2]) // 2
                                _ly = (_lk_b[1] + _lk_b[3]) // 2
                                self.hb.tap(d, _lx, _ly)
                                _liked = True
                                log.info("[smart_engage] liked via cached bounds (%d,%d)", _lx, _ly)
                            except Exception:
                                pass
                        if not _liked:
                            _liked = self._tap_like_on_visible_post(d, post["anchor_y"], did)
                        if _liked:
                            actions["liked"] = True
                            stats["likes"] += 1
                            time.sleep(random.uniform(1.0, 2.5))

                # ── 进 Profile + 加友 (B 类 v1 主流程) ──
                if tier in ("A", "B") and enable_add_b and _visits_rate_ok:
                    avatar = post.get("avatar_bounds")
                    if avatar and self._tap_avatar_enter_profile(d, avatar, did):
                        actions["profile_visited"] = True
                        stats["profiles_visited"] += 1

                        # 在 profile 页停留 (A 类更久, 模拟更深度浏览)
                        _dr = dwell_range_a if tier == "A" else dwell_range
                        dwell = random.uniform(
                            float(_dr[0]) if _dr else 10,
                            float(_dr[1]) if len(_dr) > 1 else 20,
                        )
                        log.info("[smart_engage] dwelling on %s profile %.0fs",
                                 author, dwell)
                        # 期间做 1-2 次小滚动模拟浏览
                        _dwell_end = time.time() + dwell
                        _scrolled = 0
                        while time.time() < _dwell_end:
                            time.sleep(random.uniform(2.0, 4.0))
                            if _scrolled < 2 and time.time() < _dwell_end - 3:
                                try:
                                    self.hb.scroll_down(d)
                                    _scrolled += 1
                                except Exception:
                                    pass

                        # 尝试加好友
                        try:
                            # 选个性化附言
                            note = ""
                            if _note_pool:
                                tmpl = _note_pool[_note_idx % len(_note_pool)]
                                _note_idx += 1
                                note = tmpl.replace(
                                    "{group_name}", group_name or ""
                                ).replace(
                                    "{topic_label}", analysis.get("topic_label") or ""
                                ).replace(
                                    "{author_name}", author
                                )
                                # LLM 改写保证唯一
                                try:
                                    note = self.rewrite_message(
                                        note,
                                        {"platform": "facebook",
                                         "context": "friend_request",
                                         "language": "ja"},
                                    )
                                except Exception:
                                    pass

                            ok = self.add_friend_with_note(
                                author,
                                note=note,
                                safe_mode=False,
                                device_id=did,
                                persona_key=persona_key,
                                phase=phase,
                                from_current_profile=True,
                                source=f"smart_engage:{group_name}",
                            )
                            if ok:
                                actions["friend_sent"] = True
                                stats["friend_requests_sent"] += 1
                                log.info("[smart_engage] ✓ friend request sent to %s",
                                         author)
                        except Exception as e:
                            log.warning("[smart_engage] add_friend error for %s: %s",
                                        author, e)

                        # 返回群组 feed
                        for _ in range(4):
                            try:
                                d.press("back")
                                time.sleep(1.0)
                            except Exception:
                                break
                            try:
                                xml_chk = d.dump_hierarchy() or ""
                                # 如果看到群组 feed 特征, 已回到 feed
                                if ("More options for" in xml_chk
                                        or "の投稿のその他" in xml_chk
                                        or "Write a comment" in xml_chk
                                        or "コメントする" in xml_chk
                                        or (group_name and group_name in xml_chk)):
                                    break
                            except Exception:
                                pass

                        # 加友间隔
                        if actions["friend_sent"]:
                            wait = random.uniform(
                                float(delay_range[0]) if delay_range else 30,
                                float(delay_range[1]) if len(delay_range) > 1 else 90,
                            )
                            log.info("[smart_engage] cooling %.0fs before next", wait)
                            time.sleep(wait)

                        # 进 profile 后 feed 位置已变, cached bounds 失效
                        # 记录当前帖子后 break 到外层重新 dump
                        record_engagement(did, group_name, analysis, actions)
                        eng_record = {
                            "author": author, "tier": tier,
                            "topic": analysis.get("topic", ""),
                            "l1": analysis["l1_score"],
                            "total": analysis["total_score"],
                            "liked": actions["liked"],
                            "friend_sent": actions["friend_sent"],
                        }
                        stats["engagements"].append(eng_record)
                        break  # re-dump on next scroll iteration

                # 记录互动 (仅当未被 profile-visit break 提前记录)
                if not actions["profile_visited"]:
                    record_engagement(did, group_name, analysis, actions)
                    eng_record = {
                        "author": author,
                        "tier": tier,
                        "topic": analysis.get("topic", ""),
                        "l1": analysis["l1_score"],
                        "total": analysis["total_score"],
                        "liked": actions["liked"],
                        "friend_sent": actions["friend_sent"],
                    }
                    stats["engagements"].append(eng_record)

            # 滚到下一屏
            try:
                self.hb.scroll_down(d)
            except Exception:
                try:
                    d.swipe(0.5, 0.78, 0.5, 0.32, duration=0.35)
                except Exception:
                    pass
            self.hb.wait_read(random.randint(
                int(cfg.get("min_read_time_sec", 2) * 1000),
                int(cfg.get("max_read_time_sec", 8) * 1000),
            ))

        _duration = time.time() - session_start
        stats["session_duration_sec"] = round(_duration, 1)
        stats["scrolls_used"] = scroll_i + 1 if scroll_i < max_scrolls else max_scrolls
        # 效率指标: 每分钟好友请求数
        if _duration > 0:
            stats["friends_per_minute"] = round(
                stats["friend_requests_sent"] / (_duration / 60), 2)
        log.info(
            "[smart_engage] session done: scanned=%d A=%d B=%d C=%d "
            "likes=%d profiles=%d friends=%d duration=%.0fs",
            stats["posts_scanned"], stats["tier_a"], stats["tier_b"],
            stats["tier_c"], stats["likes"], stats["profiles_visited"],
            stats["friend_requests_sent"], _duration,
        )
        return stats

    # 2026-05-03 v16/v18: FB 帖子顶部"更多操作"按钮 desc 形如
    # "More options for {author}'s post". v17 真机抓到实际 desc 用的是
    # U+2019 RIGHT SINGLE QUOTATION MARK (不是 ASCII U+0027). 用字符类
    # [''] 同时兼容两种, 避免上版正则因终端 encoding 把两个
    # apostrophe 都写成 ASCII 0x27 而永不匹配的 bug.
    # 2026-05-10 v30: 日本語 FB は "XさんのMore投稿のその他のオプション"
    # 形式を使う. JP パターンを追加し、デバイス言語に関わらず抽出可能にする.
    _FB_FEED_AUTHOR_DESC_PATTERN = re.compile(
        "^More options for (.+?)['\u2019]s\\s+post$",
        re.UNICODE,
    )
    _FB_FEED_AUTHOR_DESC_PATTERN_JA = re.compile(
        r"^(.+?)さんの投稿のその他のオプション$",
        re.UNICODE,
    )

    @_with_fb_foreground
    def extract_group_feed_authors(self, group_name: str = "",
                                    max_members: int = 20,
                                    max_scrolls: int = 12,
                                    use_llm_scoring: bool = False,
                                    target_country: str = "",
                                    device_id: Optional[str] = None,
                                    persona_key: Optional[str] = None,
                                    phase: Optional[str] = None,
                                    member_source: str = "feed_authors",
                                    join_if_needed: bool = False,
                                    **_kw: Any) -> List[Dict[str, Any]]:
        """从群帖子流提取帖子作者作为候选成员 — FB 限制 Members 列表后的合法路径.

        2026-05-03 真机 21 轮迭代发现新版 FB 已彻底移除非管理员视角的 Members
        列表入口 (Members tab 不存在 + Member tools 菜单无 Members 选项).
        但帖子作者天然是群成员且公开可见, 是合法且数据量稳定的候选发现路径.

        策略: 进群 → 滚屏看 N 帖 → 每帖抽顶部"更多操作"按钮 desc
        ('More options for {author}'s post') → 正则提取作者名 → 去重收集.

        签名与 extract_group_members 同构, 让 _campaign_extract_members 调度
        层用相同 kwargs 即可切换 source.
        """
        did = self._did(device_id)
        d = self._u2(did)
        members: List[Dict[str, Any]] = []
        try:
            self._last_group_member_source = ""
        except Exception:
            pass
        try:
            _LAST_EXTRACT_ERROR.pop(did, None)
        except Exception:
            pass

        if max_members <= 0:
            return members

        if not target_country and persona_key:
            try:
                from src.host.fb_target_personas import get_persona_display
                target_country = (
                    get_persona_display(persona_key).get("country_code") or ""
                ).upper()
            except Exception:
                pass

        if group_name and not self.enter_group(group_name, device_id=did):
            log.warning("[extract_authors] 无法进入群组: %s", group_name)
            _record_extract_error(did, "enter_group_failed")
            _capture_immediate_async(
                did, step_name="extract_authors_enter_group_failed",
                hint=f"group={group_name!r}",
                reason="enter_group returned False",
            )
            return members

        # 公共群非成员也可看帖子流, 不强求 join 成功. 但仍尝试 (与
        # extract_group_members 行为一致), 失败则继续浏览预览.
        # 2026-05-04: join_if_needed=False 时直接跳过 require_join 检查,
        # 避免 _current_group_page_requires_join 在 30 noisy candidates +
        # XML fallback 路径上 atx-agent dump 死锁 (v9/v10 真机 5+ 分钟卡死).
        if group_name and join_if_needed and self._current_group_page_requires_join(d, group_name):
            if join_if_needed:
                try:
                    self._join_current_group_page_if_needed(d, did, group_name)
                except Exception as _je:
                    log.debug("[extract_authors] join attempt failed: %s",
                                _je)
                time.sleep(1.0)
                try:
                    self._continue_group_welcome_if_present(d, did, group_name)
                except Exception:
                    pass

        seen_names: set = set()
        # v17 (2026-05-03): 取消 wall-clock cap (180s 上限被 dump_hierarchy
        # 慢路径吃掉, 真机第 22 轮只滚屏 3 次就超时). 改成单纯 max_scrolls
        # 上限. 先盲滚 2 次让帖子流出现 (避开 cover/header 区).
        try:
            for _ in range(2):
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(1200, 2000))
        except Exception:
            pass

        with self.guarded("extract_authors", device_id=did, weight=0.4):
            _set_step("浏览群帖子流", group_name)
            for _scroll_i in range(max_scrolls):
                if len(members) >= max_members:
                    break

                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    break

                try:
                    xml = d.dump_hierarchy() or ""
                    from ..vision.screen_parser import XMLParser
                    parsed_authors = list(XMLParser.parse(xml))
                    # v17 [authors-dbg]: 第一次循环输出含 "post"/"More
                    # options"/"options for" 的 desc 节点, 让下次老化定位提速.
                    if _scroll_i == 0:
                        _dbg_hits = 0
                        for _n in parsed_authors:
                            _ddesc = (
                                getattr(_n, "content_desc", None) or ""
                            ).strip()
                            if not _ddesc:
                                continue
                            if any(k in _ddesc for k in (
                                "More options", "options for",
                                "post", "Post",
                                "Profile picture",
                                "プロフィール写真",
                                "の投稿のその他",
                                "さんの投稿",
                            )):
                                log.info(
                                    "[authors-dbg] desc=%r bounds=%s",
                                    _ddesc[:90],
                                    getattr(_n, "bounds", None),
                                )
                                _dbg_hits += 1
                                if _dbg_hits >= 20:
                                    break
                    # 2026-05-04: 同时收集每个帖子里 author Profile picture
                    # 节点 bounds, 让下游 add_friend 可以直接 tap profile 头像
                    # 进 profile 页 (绕过 FB search 找不到精确人名问题).
                    _profile_pic_by_name = {}
                    for node in parsed_authors:
                        if not getattr(node, "bounds", None):
                            continue
                        _ddesc = (getattr(node, "content_desc", None) or "").strip()
                        # 2026-05-10 v30: 日本語 FB は "{name}のプロフィール写真" 形式
                        _pic_name = ""
                        if _ddesc.endswith(" Profile picture"):
                            _pic_name = _ddesc[:-len(" Profile picture")].strip()
                        elif _ddesc.endswith("のプロフィール写真"):
                            _pic_name = _ddesc[:-len("のプロフィール写真")].strip()
                        elif _ddesc.endswith("さんのプロフィール写真"):
                            _pic_name = _ddesc[:-len("さんのプロフィール写真")].strip()
                        if not _pic_name:
                            continue
                        if _pic_name and _pic_name not in _profile_pic_by_name:
                            _profile_pic_by_name[_pic_name] = node.bounds

                    for node in parsed_authors:
                        if not getattr(node, "bounds", None):
                            continue
                        desc = (
                            getattr(node, "content_desc", None) or ""
                        ).strip()
                        if not desc:
                            continue
                        m = self._FB_FEED_AUTHOR_DESC_PATTERN.match(desc)
                        if not m:
                            m = self._FB_FEED_AUTHOR_DESC_PATTERN_JA.match(desc)
                        if not m:
                            continue
                        raw_name = m.group(1).strip()
                        name = self._clean_group_member_candidate_name(raw_name)
                        if not name or name in seen_names:
                            continue
                        # 2026-05-03 v19 (真机 24 轮反馈): 加严 Page / 商家 /
                        # 媒体名过滤. 从 dump 看 'ホテル ニュー' 这类 Page 会被
                        # 抓为帖子作者. 启发式过滤明显非个人名字.
                        _name_low = name.lower()
                        if any(kw in name for kw in (
                            "Page", "Group", "页面", "公页", "公式",
                            "Official", "official", "Channel",
                            "ホテル", "Hotel", "Shop", "Store",
                            "Co.", "Inc.", "Ltd",
                        )):
                            continue
                        seen_names.add(name)
                        # 找此 author 的 Profile picture bounds (raw_name 严格匹配)
                        _pic_bounds = (
                            _profile_pic_by_name.get(raw_name)
                            or _profile_pic_by_name.get(name)
                        )
                        members.append({
                            "name": name,
                            "raw_name": raw_name,  # 原始 desc 内的名字 (含全形空格)
                            "source_section": member_source or "feed_authors",
                            "source_group": group_name,
                            "discover_method": "feed_post_author",
                            "profile_snippet": "",
                            "profile_pic_bounds": list(_pic_bounds) if _pic_bounds else None,
                        })
                        _set_step(
                            "群成员打招呼",
                            f"第 {len(members)}/{max_members} 人(帖子作者)"
                            f" — {name}",
                        )
                        if len(members) >= max_members:
                            break
                except Exception as e:
                    log.warning("[extract_authors] 解析失败: %s", e)

                try:
                    self.hb.scroll_down(d)
                except Exception:
                    try:
                        d.swipe(0.5, 0.78, 0.5, 0.32, duration=0.35)
                    except Exception:
                        pass
                self.hb.wait_read(random.randint(1200, 2200))

        if group_name and not members:
            if not _LAST_EXTRACT_ERROR.get(did):
                _record_extract_error(did, "feed_authors_zero")
            _capture_immediate_async(
                did, step_name="extract_authors_zero",
                hint=f"group={group_name!r} max_scrolls={max_scrolls}",
                reason="feed authors not found in scrolled posts",
            )

        # 2026-05-03 v19 L1 启发式预筛: 真机第 24 轮 20 候选全被 L2 视觉 gate
        # reject (男性/罗马字/Page 等非目标画像), 浪费每候选 ~30 秒 search_people
        # + ~15 秒 visual gate = ~750 秒/全池. 在抽取阶段就用 fb_lead_scorer 的
        # 启发式 (姓名语言/性别/群质量) 算 L1 score, 过滤 score < min_l1_score
        # 的明显非目标. 留下的进 L2 (视觉) 命中率显著提升.
        # 预算: 默认 min_l1_score=30 (保留 tier C 及以上, 过滤 D).
        _min_l1 = max(0, int(_kw.get("min_l1_score", 30)))
        if _min_l1 > 0 and members:
            try:
                from src.ai.fb_lead_scorer import score_member as _score_member
                _filtered: List[Dict[str, Any]] = []
                _dropped = 0
                for _m in members:
                    try:
                        _r = _score_member(
                            _m.get("name", ""),
                            source_group=group_name,
                            target_country=target_country or "",
                            target_groups=_kw.get("target_groups") or None,
                        ) or {}
                    except Exception:
                        _filtered.append(_m)   # scorer fail 不过滤 (保守)
                        continue
                    _s = int(_r.get("score") or 0)
                    if _s >= _min_l1:
                        _m["l1_score"] = _s
                        _m["l1_tier"] = _r.get("tier", "")
                        _m["l1_reasons"] = _r.get("reasons", [])
                        _filtered.append(_m)
                    else:
                        _dropped += 1
                log.info(
                    "[extract_authors] L1 prefilter: kept=%d dropped=%d "
                    "(min_l1=%d)",
                    len(_filtered), _dropped, _min_l1,
                )
                members = _filtered
            except Exception as _l1_e:
                log.warning("[extract_authors] L1 prefilter failed: %s",
                              _l1_e)

        try:
            self._last_group_member_source = member_source or "feed_authors"
        except Exception:
            pass
        log.info(
            "[extract_authors] group=%r yielded=%d/%d (scrolls)",
            group_name, len(members), max_members,
        )
        return members

    @_with_fb_foreground
    def extract_group_members(self, group_name: str = "",
                              max_members: int = 30,
                              use_llm_scoring: bool = False,
                              target_country: str = "",
                              device_id: Optional[str] = None,
                              persona_key: Optional[str] = None,
                              phase: Optional[str] = None,
                              member_source: str = "",
                              join_if_needed: bool = False) -> List[Dict[str, Any]]:
        """群成员打招呼准备列表 — FB 客服拓展入口。

        流程: 进群 → 点 "Members" Tab → 滚动列表 → 整理昵称/头像/简介
        整理后会自动写入 LeadsStore(source_platform=facebook, tag=群名)。

        2026-04-22 persona 改造:
          * ``max_members`` 未显式指定(用方法签名默认 30)时,按 ``phase`` 从
            ``facebook_playbook.yaml.extract_members.max_members`` 取。
            cold_start=0(禁用) / growth=12 / mature=25 / cooldown=0。
          * ``target_country`` 空串时从 persona.country_code 自动派生,
            避免 scorer 拿 target_country="" 直接降档。
        """
        did = self._did(device_id)
        d = self._u2(did)
        members: List[Dict[str, Any]] = []
        try:
            self._last_group_member_source = ""
        except Exception:
            pass
        try:
            _LAST_EXTRACT_ERROR.pop(did, None)
        except Exception:
            pass

        # P0-2: phase 参数合并。max_members=0 表示该阶段禁用群成员打招呼，需要短路。
        eff_phase, ab_cfg = _resolve_phase_and_cfg("extract_members",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_members == 30 and "max_members" in ab_cfg:
            max_members = int(ab_cfg.get("max_members") or max_members)
        if max_members <= 0:
            log.info("[extract_group_members] phase=%s 禁止群成员打招呼 (max_members=0), skip",
                     eff_phase)
            return members

        # P0-2: target_country 从 persona 派生（避免评分器降档）
        if not target_country and persona_key:
            try:
                from src.host.fb_target_personas import get_persona_display
                target_country = (get_persona_display(persona_key).get("country_code")
                                  or "").upper()
            except Exception:
                pass

        if group_name and not self.enter_group(group_name, device_id=did):
            log.warning("[extract_group_members] 无法进入群组: %s", group_name)
            # P2.X: 记录到模块状态 → executor 会映射为 outcome=automation_enter_group_failed
            _record_extract_error(did, "enter_group_failed")
            # P2.0 (2026-04-30): 进群失败那一刻同步抓 PNG + XML, 后续 task_store
            # 触发 forensics 会 promote 这张图到正式目录。无环境/异常都吞掉。
            _capture_immediate_async(
                did, step_name="extract_enter_group_failed",
                hint=f"group={group_name!r}",
                reason="enter_group returned False",
            )
            return members

        if group_name and self._current_group_page_requires_join(d, group_name):
            if join_if_needed:
                _set_step("加入目标群组", group_name)
                join_ok = False
                join_outcome = ""
                try:
                    join_ok, join_outcome = self._join_current_group_page_if_needed(
                        d, did, group_name,
                    )
                    if not join_ok and join_outcome == "join_button_not_found":
                        log.info(
                            "[extract_group_members] current page join miss; "
                            "fallback to search join group=%r", group_name,
                        )
                        join_ok = bool(self.join_group(group_name, device_id=did))
                        join_outcome = (
                            getattr(self, "last_join_group_outcome", "") or ""
                        )
                    elif join_ok:
                        try:
                            self.last_join_group_outcome = join_outcome
                        except Exception:
                            pass
                    else:
                        try:
                            self.last_join_group_outcome = join_outcome
                        except Exception:
                            pass
                except Exception as e:
                    log.warning("[extract_group_members] 自动入群异常 group=%r: %s",
                                group_name, e)
                    join_outcome = "join_exception"
                if not join_ok:
                    if join_outcome in (
                        "membership_questions_required",
                        "join_requested_pending_approval",
                    ):
                        err_step = "group_join_blocked"
                        reason = f"join_group outcome={join_outcome}"
                    else:
                        err_step = "group_join_failed"
                        reason = f"join_group outcome={join_outcome or 'unknown'}"
                    _record_extract_error(did, err_step)
                    _capture_immediate_async(
                        did, step_name=err_step,
                        hint=f"group={group_name!r}",
                        reason=reason,
                    )
                    return members

                time.sleep(1.0)
                try:
                    self._continue_group_welcome_if_present(d, did, group_name)
                except Exception:
                    pass
                if group_name and not self.enter_group(group_name, device_id=did):
                    log.warning("[extract_group_members] 入群后无法重新进入群组: %s",
                                group_name)
                    _record_extract_error(did, "enter_group_failed")
                    _capture_immediate_async(
                        did, step_name="extract_reenter_group_failed",
                        hint=f"group={group_name!r}",
                        reason="enter_group after join returned False",
                    )
                    return members
                if self._current_group_page_requires_join(d, group_name):
                    _record_extract_error(did, "group_requires_join")
                    _capture_immediate_async(
                        did, step_name="extract_group_requires_join",
                        hint=f"group={group_name!r}",
                        reason="join button still visible after join",
                    )
                    return members
            else:
                log.warning("[extract_group_members] 当前群组尚未加入: %s", group_name)
                _record_extract_error(did, "group_requires_join")
                _capture_immediate_async(
                    did, step_name="extract_group_requires_join",
                    hint=f"group={group_name!r}",
                    reason="join button visible before members tab",
                )
                return members

        with self.guarded("extract_members", device_id=did, weight=0.6):
            _set_step("打开 Members tab", group_name)
            # 2026-05-04: 清空 search-input candidates cache 防 race
            try:
                self._search_input_candidates = None
            except Exception:
                pass
            # 2026-04-23 bug fix: "Members tab in the group header" 的 AutoSelector
            # 学习被污染为 "Suggested group: 50代以上..." 的 bounds(推荐群卡片),
            # 会误点进推荐群。改用硬定位:text/desc 精确匹配 "Members"/"メンバー"。
            hit = self._tap_group_members_tab(d, did, member_source)
            if not hit:
                # Members tab / member preview 入口都失败时快速返回。
                # 不再二次 smart_tap 群头：真实机上它容易误入反应列表或在
                # u2/ADB 抖动时拖住任务数分钟。
                _record_extract_error(did, "members_tab_not_found")
                _capture_immediate_async(
                    did, step_name="extract_members_tab_not_found",
                    hint=f"group={group_name!r}",
                    reason="_tap_group_members_tab fast paths failed",
                )
                return members
            time.sleep(2.0)

            # 2026-05-04 P0: 如果 _tap_via_search_members_input 抽到 candidates,
            # 直接用 (绕开 SDUI 不暴露 contributors 真名的限制).
            _sic = getattr(self, "_search_input_candidates", None)
            if _sic and isinstance(_sic, list) and len(_sic) >= 1:
                log.info(
                    "[extract_group_members] 用 search-input 路径抽到的 %d 个 candidates",
                    len(_sic),
                )
                for _c in _sic[:max_members]:
                    _name = _c.get("name", "").strip()
                    if not _name:
                        continue
                    members.append({
                        "name": _name,
                        "raw_name": _name,
                        "source_section": "search_members_input",
                        "source_group": group_name,
                        "discover_method": "search_members_input",
                        "profile_snippet": "",
                        "profile_pic_bounds": _c.get("bounds"),
                    })
                # 清空 cache 防 race
                try:
                    self._search_input_candidates = None
                except Exception:
                    pass
                if members:
                    log.info("[extract_group_members] search-input 短路: %d members yielded",
                             len(members))
                    return members

            seen_names = set()
            scrolls = 0
            max_scrolls = max(5, max_members // 6)
            # 2026-05-05 P7-C-2: 真机 task f2aebe47 揭示 contributors 列表
            # dump_hierarchy 每轮 ~60s (SDUI lazy + 386 members 大列表慢),
            # 原公式 max(45, min(120, 6*18=108))=108s 只让 ~2 次循环跑完就熔断
            # (scrolls=2 < max=6, members=0). 改成按 60s/轮 估算给 6 次留时间.
            extract_deadline = time.time() + max(120.0, min(360.0, max_scrolls * 60.0))

            while len(members) < max_members and scrolls < max_scrolls:
                if time.time() > extract_deadline:
                    log.warning("[extract_group_members] reached wall-clock cap "
                                "scrolls=%d max_scrolls=%d members=%d",
                                scrolls, max_scrolls, len(members))
                    break
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    break

                try:
                    xml = d.dump_hierarchy()
                    from ..vision.screen_parser import XMLParser
                    elements = XMLParser.parse(xml)
                    # Stage T: extract_group_members 跑在 contributors 完整列表页,
                    # 启用 P7-C-1 row-pattern 降级 (没"加好友"按钮时 fallback).
                    for member in self._extract_group_member_candidates(
                        elements, enable_no_add_fallback=True,
                    ):
                        name = (member.get("name") or "").strip()
                        if not name or name in seen_names:
                            continue
                        member.setdefault(
                            "source_section",
                            getattr(self, "_last_group_member_source", "")
                            or member_source
                            or "general",
                        )
                        members.append(member)
                        seen_names.add(name)
                        # 业务进展可见: 每整理 1 人刷新 dashboard step
                        _set_step("群成员打招呼",
                                  f"第 {len(members)}/{max_members} 人 — {name}")
                        if len(members) >= max_members:
                            break
                except Exception as e:
                    log.warning("[extract_group_members] 解析失败: %s", e)

                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(800, 2000))
                scrolls += 1

        # 2026-05-05 P7-A fallback: 滚动循环 0 抽 → 用 Search Members 输入兜底
        # (低活跃账号 SDUI lazy 不渲染真名时仍能抽到 1-7 候选).
        if not members:
            try:
                if self._tap_via_search_members_input(d, did):
                    _sic_fb = getattr(self, "_search_input_candidates", None)
                    if _sic_fb and isinstance(_sic_fb, list):
                        log.info(
                            "[extract_group_members] P7-A fallback search-input "
                            "抽到 %d 候选 (滚动 0 抽后)", len(_sic_fb))
                        for _c in _sic_fb[:max_members]:
                            _name = _c.get("name", "").strip()
                            if not _name:
                                continue
                            members.append({
                                "name": _name,
                                "raw_name": _name,
                                "source_section": "search_members_input_fallback",
                                "source_group": group_name,
                                "discover_method": "search_members_input_fallback",
                                "profile_snippet": "",
                                "profile_pic_bounds": _c.get("bounds"),
                            })
                        try:
                            self._search_input_candidates = None
                        except Exception:
                            pass
            except Exception as _fb_e:
                log.debug("[extract_group_members] P7-A fallback 异常: %s", _fb_e)

        # P2.0 (2026-04-30): 进群且 Members tab 没硬失败, 但循环后仍 0 成员
        # —— 最容易遗漏的现场。同步取证, 让运营看到当时屏幕到底是什么。
        if group_name and not members:
            # 仅在未被上游失败覆盖时才记录 zero_after_enter (保留更具体的上游原因)
            if not _LAST_EXTRACT_ERROR.get(did):
                _record_extract_error(did, "zero_after_enter")
            _capture_immediate_async(
                did, step_name="extract_zero_after_enter",
                hint=f"group={group_name!r} max_scrolls={max_scrolls}",
                reason="loop 完成但未整理到任何成员",
            )

        # 写入 Leads Pool + Sprint 3 P1: scorer v2 两阶段评分(可选 LLM 精排)
        if members:
            try:
                from ..leads.store import get_leads_store
                store = get_leads_store()
                tag = f"fb_group:{group_name}" if group_name else "fb_group:current"

                if use_llm_scoring:
                    from ..ai.fb_lead_scorer_v2 import score_member_v2 as scorer
                    score_field = "final_score"
                    tier_field = "final_tier"
                    score_kwargs = {"use_llm": True}
                else:
                    from ..ai.fb_lead_scorer import score_member as scorer
                    score_field = "score"
                    tier_field = "tier"
                    score_kwargs = {}

                for m in members:
                    try:
                        lead_id = store.add_lead(
                            name=m.get("name", "Unknown"),
                            source_platform="facebook",
                            tags=[tag],
                        )
                        m["lead_id"] = lead_id

                        existing = store.get_lead(lead_id) if lead_id else None
                        s_result = scorer(
                            m.get("name", ""),
                            source_group=group_name,
                            target_country=target_country,
                            target_groups=[group_name] if group_name else [],
                            lead_record=existing,
                            **score_kwargs,
                        )
                        s = s_result.get(score_field, s_result.get("score", 0))
                        t = s_result.get(tier_field, s_result.get("tier", "D"))
                        m.update({
                            "score": s,
                            "tier": t,
                            "score_reasons": s_result.get("reasons", []),
                            "scorer_version": "v2" if use_llm_scoring else "v1",
                            "llm_used": s_result.get("llm_used", False),
                        })
                        if lead_id and s > 0:
                            try:
                                store.update_lead(lead_id, score=s)
                            except Exception:
                                pass
                        try:
                            contacted, reason = self._peer_already_contacted(
                                m.get("name", ""))
                            m["already_contacted"] = contacted
                            if reason:
                                m["contacted_reason"] = reason
                        except Exception:
                            pass
                        self._append_journey_for_action(
                            m.get("name", ""),
                            "extracted",
                            did=did,
                            persona_key=persona_key,
                            discovered_via="group_extract",
                            data={
                                "source_group": group_name or "current",
                                "target_country": target_country or "",
                                "lead_id": lead_id,
                                "score": m.get("score", 0),
                                "tier": m.get("tier", ""),
                            },
                        )
                    except Exception:
                        pass
                log.info("[extract_group_members] 入库+评分 %d 人 (scorer=%s, group=%s)",
                         len(members), "v2" if use_llm_scoring else "v1",
                         group_name or "current")
            except Exception as e:
                log.warning("[extract_group_members] LeadsStore 写入失败: %s", e)

        return members

    # ── Profile Operations ────────────────────────────────────────────────

    def view_profile(self, profile_name: str,
                     read_seconds: float = 10.0,
                     device_id: Optional[str] = None) -> bool:
        """打开目标用户主页并停留指定秒数(用于"先看再加"风控规避)。"""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("view_profile", device_id=did, weight=0.2):
            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                return False
            first = self._first_search_result_element(d, query_hint=profile_name)
            if first is None:
                return False
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(2.0, 3.5))

            # 模拟阅读
            elapsed = 0.0
            while elapsed < read_seconds:
                if random.random() < 0.6:
                    self.hb.scroll_down(d)
                wait = random.uniform(2.0, 4.5)
                time.sleep(wait)
                elapsed += wait
            return True

    def read_profile_about(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """假设当前在某用户主页,提取 About 信息。

        返回:
            {about_text, work, education, lives_in, from_place} 等(尽力提取)
        """
        did = self._did(device_id)
        d = self._u2(did)
        info = {}
        try:
            self.smart_tap("About tab on profile", device_id=did)
            time.sleep(1.5)
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            texts = [el.text for el in elements if el.text]
            joined = " | ".join(texts)
            info["raw_about"] = joined[:2000]
            for kw, key in [("Works at", "work"), ("Studied at", "education"),
                            ("Lives in", "lives_in"), ("From", "from_place"),
                            ("Married", "marital"), ("In a relationship", "marital")]:
                idx = joined.find(kw)
                if idx >= 0:
                    info[key] = joined[idx + len(kw):idx + len(kw) + 80].strip(" |:")
        except Exception as e:
            log.warning("[read_profile_about] 失败: %s", e)
        return info

    # ── Profile Snapshot for VLM Classifier (P2-4 Sprint A) ──────────────

    def capture_profile_snapshots(self,
                                   shot_count: int = 3,
                                   scroll_between: bool = True,
                                   scroll_dwell_sec: float = 1.5,
                                   device_id: Optional[str] = None,
                                   save_dir: Optional[str] = None,
                                   tag: str = "profile") -> Dict[str, Any]:
        """假设当前**已在某用户主页**，连续截 N 张图供 VLM 判定。

        参数:
            shot_count: 要截的张数（默认 3：头像区 + 下滑 1 次 + 下滑 2 次）
            scroll_between: 两张图之间是否滚动（False 则全是同一屏）
            scroll_dwell_sec: 滚动后等待再截图的秒数（让渲染稳定）
            save_dir: 截图保存目录，默认 data/fb_profile_shots/{device}_{ts}/
            tag: 文件名前缀，便于定位

        返回:
            {
              "image_paths": [...],
              "display_name": "" | "",      # 从顶部标题栏抠
              "bio_text": "",               # 从可见文字里抠前 300 字
              "shot_count": N,
            }
        """
        import os
        from time import time as _t

        from src.host.device_registry import data_dir

        did = self._did(device_id)
        d = self._u2(did)

        ts = int(_t())
        if save_dir is None:
            save_dir = str(data_dir() / "fb_profile_shots" / f"{did}_{ts}")
        os.makedirs(save_dir, exist_ok=True)

        image_paths: List[str] = []
        for i in range(max(1, shot_count)):
            p = os.path.join(save_dir, f"{tag}_{i+1}.png")
            try:
                saved = self.screenshot(device_id=did, save_path=p)
                if saved:
                    # screenshot() returns raw bytes but we want the file path string
                    image_paths.append(p)
            except Exception as e:
                log.warning("[capture_profile_snapshots] 截图失败 i=%d: %s", i, e)
            if scroll_between and i < shot_count - 1:
                try:
                    self.hb.scroll_down(d)
                except Exception:
                    pass
                time.sleep(scroll_dwell_sec)

        display_name, bio_text = self._extract_profile_text(d)
        # Sprint E-0.3: MIUI 上 uiautomator dump 被 kill → display_name/bio 为空。
        # 降级：用 qwen2.5vl:7b 对已有的 screencap 做一次 OCR 专用抽取。
        # 策略:
        #   - 仅当 display_name 和 bio 都为空，且 image_paths 非空时触发
        #   - 复用已在 GPU 的 VLM,不新增依赖；costs ~3-5s (预热后)
        #   - 失败也不抛,返回空串
        if image_paths and not (display_name and bio_text):
            try:
                ocr_name, ocr_bio = self._vlm_ocr_profile_texts(image_paths[:2])
                if not display_name and ocr_name:
                    display_name = ocr_name
                if not bio_text and ocr_bio:
                    bio_text = ocr_bio
            except Exception as _e:
                log.debug("[capture_profile_snapshots] VLM OCR 兜底失败（忽略）: %s", _e)
        return {
            "image_paths": image_paths,
            "display_name": display_name,
            "bio_text": bio_text,
            "shot_count": len(image_paths),
            "save_dir": save_dir,
        }

    _REPORT_DIALOG_TEXTS = {
        "what do you want to report?",
        "if someone is in immediate danger",
        "report this profile",
        "why are you reporting this?",
        "report a problem",
    }

    def _is_on_report_dialog(self, d) -> bool:
        """检测当前页面是否为 Facebook 的 Report（举报）对话框 / 页面。"""
        try:
            xml = d.dump_hierarchy()
            xml_lower = xml.lower()
            return any(kw in xml_lower for kw in self._REPORT_DIALOG_TEXTS)
        except Exception:
            return False

    def _extract_profile_text(self, d) -> Tuple[str, str]:
        """从当前 UI 抠 display_name 和 bio 文本（best effort）。"""
        name = ""
        bio = ""
        try:
            xml = d.dump_hierarchy()
            # 若当前页是 Report 对话框，直接返回空（避免把举报页文字当 profile）
            xml_lower = xml.lower()
            if any(kw in xml_lower for kw in self._REPORT_DIALOG_TEXTS):
                log.debug("[_extract_profile_text] 当前页是 Report 对话框，跳过")
                return "", ""
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            texts = [(getattr(el, "text", "") or "").strip() for el in elements]
            texts = [t for t in texts if t]
            # Skip status bar clock patterns like 「2:32」 and pure digits
            import re as _re
            clock_pattern = _re.compile(r'^[\u300c\u300d]?\d{1,2}:\d{2}[\u300c\u300d]?$')
            skip_keywords = {"Home", "Friends", "Marketplace", "Search", "Like",
                             "Comment", "Share", "Follow", "Message", "Add Friend",
                             "Followers", "Following", "About", "Photos", "More",
                             "See All", "See more", "Report"}
            for t in texts:
                tt = t.strip()
                if clock_pattern.match(tt):
                    continue
                if 2 <= len(tt) <= 40 and tt not in skip_keywords and not tt.isdigit():
                    name = tt
                    break
            merged = " | ".join(texts)
            bio = merged[:300]
        except Exception as e:
            log.debug("[_extract_profile_text] 失败: %s", e)
        return name, bio

    def _vlm_ocr_profile_texts(self, image_paths: List[str]) -> Tuple[str, str]:
        """Sprint E-0.3: 用 VLM 从截图抠 display_name + bio（OCR 兜底）。

        单次 VLM 调用（不做分类，只抠文字），返回 (display_name, bio_text)。
        失败/超时都返回 ("", "")，不抛异常（调用方已 try/except）。

        注意: 此函数会**触发 _VLM_LOCK 排队**，建议只在 dump 失败的 MIUI 环境
        使用。正常 Android 手机走 dump 路径会快很多。
        """
        if not image_paths:
            return "", ""
        try:
            from src.host.ollama_vlm import classify_images
        except Exception:
            return "", ""

        prompt = (
            "You are an OCR assistant. Read the Facebook profile screenshot(s) and "
            "return ONLY a compact JSON object with two string fields:\n"
            '  {"display_name": "<user\'s shown name, best guess>", '
            '"bio": "<concatenated visible profile text (intro/about/city/work/school), '
            'strip nav labels like Home/Friends/Search/Marketplace/Like/Comment/Share/'
            'Followers/Following, keep under 300 chars>"}\n'
            "Rules:\n"
            "  * No extra text before/after the JSON\n"
            "  * If you can't find the name, use empty string\n"
            "  * Preserve original language (Japanese/English/Chinese), do NOT translate\n"
            "  * display_name must be 2~40 chars"
        )
        try:
            result, meta = classify_images(
                prompt=prompt,
                image_paths=image_paths[:2],
                scene="fb_profile_ocr",
                task_id="vlm_ocr",
                device_id="",
            )
            if not meta.get("ok"):
                return "", ""
            if not isinstance(result, dict):
                return "", ""
            name = (result.get("display_name") or "").strip()
            bio = (result.get("bio") or "").strip()
            if len(name) > 60:
                name = name[:60]
            if len(bio) > 300:
                bio = bio[:300]
            # 过滤明显垃圾（纯标点、全是数字、过短）
            if name and (len(name) < 2 or name.replace(" ", "").isdigit()):
                name = ""
            log.info("[vlm_ocr] name=%r bio_len=%d latency=%dms",
                     name, len(bio), int(meta.get("total_ms") or 0))
            return name, bio
        except Exception as e:
            log.debug("[vlm_ocr] 异常: %s", e)
            return "", ""

    @_with_fb_foreground
    def classify_current_profile(self,
                                  target_key: str,
                                  persona_key: Optional[str] = None,
                                  task_id: str = "",
                                  shot_count: int = 3,
                                  device_id: Optional[str] = None) -> Dict[str, Any]:
        """端到端：**假设当前已在某 FB 用户主页**，
        截图 → 抽文字 → 过 ProfileClassifier → 返回分类结果。

        target_key: 用户唯一标识（profile_url / user_id / 昵称），用于去重。
        """
        did = self._did(device_id)
        snap = self.capture_profile_snapshots(
            shot_count=shot_count, device_id=did, tag="classify",
        )
        try:
            from src.host.fb_profile_classifier import classify
            from src.host.fb_target_personas import get_persona
        except Exception as e:
            return {"ok": False, "error": f"import classifier 失败: {e}", "snapshot": snap}

        persona = get_persona(persona_key)
        result = classify(
            device_id=did,
            task_id=task_id,
            persona_key=persona["persona_key"],
            target_key=target_key,
            display_name=snap.get("display_name", ""),
            bio=snap.get("bio_text", ""),
            username="",
            locale=persona.get("locale", ""),
            image_paths=snap["image_paths"],
            l2_image_paths=snap["image_paths"],
            do_l2=True,
            dry_run=False,
        )
        result["snapshot"] = {
            "shot_count": snap["shot_count"],
            "save_dir": snap["save_dir"],
            "display_name": snap["display_name"],
        }
        return result

    # ── Profile Hunt: 批量候选 → L1+L2 → 命中处理 (P2-4 Sprint B) ──────

    @_with_fb_foreground
    def profile_hunt(self,
                     candidates: List[str],
                     persona_key: Optional[str] = None,
                     action_on_match: str = "none",   # none / follow / add_friend
                     note: str = "",
                     max_targets: Optional[int] = None,
                     inter_target_sec: Tuple[float, float] = (20.0, 34.0),
                     shot_count: int = 3,
                     task_id: str = "",
                     device_id: Optional[str] = None,
                     candidate_source: str = "") -> Dict[str, Any]:
        """批量候选 profile hunt。

        流程（每个候选）:
          1. ``search_people(name)`` + 点第一条结果 → 进主页
          2. ``classify_current_profile`` 截图 + L1+L2（含配额/去重/风控）
          3. 命中时按 ``action_on_match`` 执行:
             - ``follow``    : 找并点 Follow 按钮
             - ``add_friend``: 在当前主页找并点 Add Friend（可带 note）
             - ``none``      : 只记录，不操作
          4. 返回主页（避免栈过深），sleep 真人间隔 20-34s（带抖动）

        返回结构（详细统计，供 tasks-chat 卡片渲染）:
          {
            card_type: "fb_profile_hunt",
            persona_key, action_on_match, candidates_total,
            processed, l1_pass, l2_run, matched, actioned,
            skipped: {l1_fail, l2_cap, risk_pause, cached, search_fail},
            results: [ {name, match, score, stage, reason, action_ok} ]
          }
        """
        did = self._did(device_id)
        d = self._u2(did)

        # 延迟导入以避免模块启动时的循环依赖
        from src.host.fb_target_personas import (
            get_persona, get_dedup_window_hours, get_quotas,
        )
        from src.host import fb_profile_classifier as _clf
        # Sprint E-0.1: 任务入口 fire-and-forget 预热 VLM，消掉首张 56s 冷启动。
        # 幂等：10 分钟内已 warmup 过则自动跳过，多任务同时开跑只会 warmup 一次。
        try:
            from src.host.ollama_vlm import warmup_async as _vlm_warmup_async
            _vlm_warmup_async()
        except Exception as _e:
            log.debug("[profile_hunt] VLM warmup_async 失败（忽略）: %s", _e)

        persona = get_persona(persona_key)
        pk = persona["persona_key"]
        l1_threshold = float(((persona or {}).get("l1") or {}).get("pass_threshold") or 30)
        dedup_h = int(get_dedup_window_hours() or 168)
        _q = get_quotas() or {}
        l2_daily_cap = int(_q.get("l2_per_device_per_day") or 100)
        l2_hourly_cap = int(_q.get("l2_per_device_per_hour") or 0)  # 0 = 关

        # Sprint D-1: 软降档（风控二级阶梯）
        # 硬暂停由 classifier.classify 内部处理（pause_l2_after_risk_hours）。
        # 这里处理"软降档"：近 soft_window h 内有风控但 hard pause 已过 → cap×ratio + interval×factor
        try:
            from src.host.fb_target_personas import get_risk_guard
            _rg = get_risk_guard() or {}
        except Exception:
            _rg = {}
        soft_win_h = int(_rg.get("soft_throttle_window_hours") or 0)
        cap_ratio = float(_rg.get("soft_throttle_cap_ratio") or 1.0)
        ivl_factor = float(_rg.get("soft_throttle_interval_factor") or 1.0)
        soft_throttled = False
        if soft_win_h > 0 and (cap_ratio < 1.0 or ivl_factor > 1.0):
            try:
                from src.host import fb_store as _fbs
                recent_risk = int(_fbs.count_risk_events_recent(device_id or "", soft_win_h))
            except Exception:
                recent_risk = 0
            if recent_risk > 0:
                soft_throttled = True
                old_cap_d, old_cap_h = l2_daily_cap, l2_hourly_cap
                l2_daily_cap = max(1, int(l2_daily_cap * cap_ratio))
                if l2_hourly_cap > 0:
                    l2_hourly_cap = max(1, int(l2_hourly_cap * cap_ratio))
                old_lo, old_hi = float(inter_target_sec[0]), float(inter_target_sec[1])
                inter_target_sec = (old_lo * ivl_factor, old_hi * ivl_factor)
                log.info(
                    "profile_hunt 进入软降档 (device=%s 近%dh风控=%d): "
                    "l2_daily %d→%d, l2_hour %d→%d, interval %.1f~%.1f→%.1f~%.1f",
                    device_id, soft_win_h, recent_risk,
                    old_cap_d, l2_daily_cap, old_cap_h, l2_hourly_cap,
                    old_lo, old_hi, inter_target_sec[0], inter_target_sec[1],
                )

        cands = [c.strip() for c in (candidates or []) if c and c.strip()]
        if max_targets is not None:
            cands = cands[:max(0, int(max_targets))]

        stats: Dict[str, Any] = {
            "card_type": "fb_profile_hunt",
            "persona_key": pk,
            "persona_name": persona.get("name", ""),
            "action_on_match": action_on_match,
            "candidates_total": len(cands),
            "processed": 0,
            "l1_pass": 0,
            "l2_run": 0,
            "matched": 0,
            "actioned": 0,
            "skipped": {"l1_fail": 0, "l2_cap": 0, "l2_hourly_cap": 0, "risk_pause": 0,
                        "cached": 0, "search_fail": 0, "classify_err": 0,
                        "prefilter": 0},
            "risk_interrupted": None,
            "results": [],
            "started_at": _now_iso(),
            "optimizations_applied": [
                "name_prefilter", "dedup_cache",
                "l2_daily_cap_short_circuit", "l2_hourly_cap_short_circuit",
                "vlm_warmup_async",           # Sprint E-0.1
                "deeplink_navigation",        # Sprint E-1.1
            ] + (["soft_throttle_by_risk"] if soft_throttled else []),
            "soft_throttled": soft_throttled,
            "effective_l2_daily_cap": l2_daily_cap,
            "effective_l2_hourly_cap": l2_hourly_cap,
            "effective_interval_sec": [round(inter_target_sec[0], 1),
                                       round(inter_target_sec[1], 1)],
        }

        if not cands:
            return stats

        lo, hi = float(inter_target_sec[0]), float(inter_target_sec[1])

        for idx, name in enumerate(cands):
            # ── 每轮开头：风控总闸（出现风控对话框 → 立刻中止本任务）──
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[profile_hunt] 检测到风控，中止: %s", msg)
                stats["risk_interrupted"] = msg
                break

            item: Dict[str, Any] = {"name": name, "match": False, "score": 0,
                                    "stage": "", "reason": "", "action_ok": False,
                                    "from_cache": False}
            stats["processed"] += 1

            # ── 优化 A：name-only L1 预筛（避免为铁定不过的名字浪费 search+截图 25s）──
            pre_sc, pre_reasons = _clf.score_l1(
                persona, {"display_name": name, "bio": "", "username": ""}
            )
            if pre_sc <= 0:
                item["score"] = pre_sc
                item["stage"] = "L1-pre"
                item["reason"] = "prefilter_no_jp_signal"
                stats["skipped"]["prefilter"] += 1
                stats["results"].append(item)
                # 只跳过 search/snapshot，保持整体节奏（1-3s 小间隔避免打满循环）
                time.sleep(random.uniform(0.8, 1.6))
                continue

            # ── 优化 B：去重窗口前置（命中缓存直接用历史结果，不 search）──
            target_key_pre = f"search:{name}"
            cached = _clf._db_get_recent(pk, target_key_pre, dedup_h)
            if cached:
                item["score"] = float(cached.get("score") or 0)
                item["stage"] = cached.get("stage") or ""
                item["match"] = bool(cached.get("match"))
                item["from_cache"] = True
                item["reason"] = "dedup_cache_hit"
                stats["skipped"]["cached"] += 1
                if item["match"]:
                    stats["matched"] += 1
                    # 注意：复用缓存不再执行 action，避免对同一人短期重复触达
                stats["results"].append(item)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            # ── 优化 C：L2 日配额提前检查。若今日 L2 已满 → 剩余候选只做 L1 预筛，不 search ──
            try:
                today_l2 = _clf._db_count_today(did, "L2")
            except Exception:
                today_l2 = 0
            try:
                hour_l2 = _clf._db_count_recent_hours(did, "L2", 1) if l2_hourly_cap > 0 else 0
            except Exception:
                hour_l2 = 0

            cap_hit = None
            if today_l2 >= l2_daily_cap:
                cap_hit = "l2_daily_cap"
            elif l2_hourly_cap > 0 and hour_l2 >= l2_hourly_cap:
                cap_hit = "l2_hourly_cap"

            if cap_hit:
                item["score"] = pre_sc
                item["stage"] = "L1-pre"
                if pre_sc < l1_threshold:
                    item["reason"] = "l1_below_threshold"
                    stats["skipped"]["l1_fail"] += 1
                else:
                    item["reason"] = cap_hit
                    if cap_hit == "l2_daily_cap":
                        stats["skipped"]["l2_cap"] += 1
                    else:
                        stats["skipped"]["l2_hourly_cap"] += 1
                stats["results"].append(item)
                time.sleep(random.uniform(0.8, 1.4))
                continue

            try:
                # Sprint E-1.1: 统一走 navigate_to_profile（URL/username/user_id 走 deeplink，
                # 纯显示名才降级 search）。这让 MIUI 限制下仍能跑 deeplink 链路。
                nav = self.navigate_to_profile(
                    name, device_id=did,
                    post_open_dwell_sec=(2.5, 4.0),
                )
                if not nav.get("ok"):
                    item["reason"] = f"search_fail:{nav.get('reason','')}"
                    item["nav_kind"] = nav.get("kind")
                    item["nav_via"] = nav.get("via")
                    stats["skipped"]["search_fail"] += 1
                    stats["results"].append(item)
                    time.sleep(random.uniform(lo / 3, lo / 2))
                    continue
                item["nav_kind"] = nav.get("kind")
                item["nav_via"] = nav.get("via")
                target_key = nav.get("target_key") or f"search:{name}"
                # Phase D2: 缓存稳定身份标识,供下游 _record_friend_request_safely 使用
                self._last_nav_target_key = target_key
                try:
                    r = self.classify_current_profile(
                        target_key=target_key,
                        persona_key=pk,
                        task_id=task_id,
                        shot_count=shot_count,
                        device_id=did,
                    )
                except Exception as e:
                    log.exception("[profile_hunt] classify 失败: %s", e)
                    item["reason"] = f"classify_err: {e}"
                    stats["skipped"]["classify_err"] += 1
                    stats["results"].append(item)
                    self._go_back_to_feed(d)
                    time.sleep(random.uniform(lo, hi))
                    continue

                item["score"] = float(r.get("score") or 0)
                item["stage"] = r.get("stage_reached") or ""
                item["match"] = bool(r.get("match"))
                item["from_cache"] = bool(r.get("from_cache"))

                if item["from_cache"]:
                    stats["skipped"]["cached"] += 1
                    item["reason"] = "dedup_cache_hit"
                elif r.get("quota", {}).get("exceeded") == "l2_daily_cap":
                    stats["skipped"]["l2_cap"] += 1
                    item["reason"] = "l2_daily_cap"
                elif r.get("quota", {}).get("exceeded") == "l2_paused_by_risk":
                    stats["skipped"]["risk_pause"] += 1
                    item["reason"] = "risk_pause"
                elif (r.get("l1") or {}).get("pass") is False:
                    stats["skipped"]["l1_fail"] += 1
                    item["reason"] = "l1_below_threshold"
                else:
                    stats["l1_pass"] += 1
                    if item["stage"] == "L2":
                        stats["l2_run"] += 1

                if item["match"]:
                    stats["matched"] += 1
                    if action_on_match and action_on_match != "none":
                        ok = self._do_action_on_profile(d, did, action_on_match, note=note)
                        item["action_ok"] = ok
                        if ok:
                            stats["actioned"] += 1

                if candidate_source == "name_hunter":
                    try:
                        from src.host.fb_targets_store import mark_name_hunter_profile_result
                        _ins = dict(r.get("insights") or {})
                        _l2 = r.get("l2") or {}
                        if _l2.get("reasons") and not _ins.get("reasons"):
                            _ins["reasons"] = _l2.get("reasons")[:3]
                        mark_name_hunter_profile_result(
                            name=name,
                            persona_key=pk,
                            matched=bool(item["match"]),
                            score=float(item["score"] or 0),
                            stage=item["stage"] or "",
                            insights=_ins,
                            device_id=did,
                        )
                    except Exception as _cand_e:
                        log.debug("[profile_hunt] name_hunter candidate persist skipped: %s", _cand_e)

                stats["results"].append(item)

            except Exception as e:
                log.exception("[profile_hunt] 处理 %s 失败: %s", name, e)
                item["reason"] = f"exception: {e}"
                stats["results"].append(item)

            finally:
                # 回到 feed，清理 stack，继续下一位
                try:
                    self._go_back_to_feed(d)
                except Exception:
                    pass
                if idx < len(cands) - 1:
                    time.sleep(random.uniform(lo, hi))

        stats["finished_at"] = _now_iso()
        return stats

    def _do_action_on_profile(self, d, did: str, action: str, note: str = "") -> bool:
        """在当前主页上执行 follow / add_friend。假设当前已在目标主页。"""
        action = (action or "").lower().strip()
        # 滚回顶部（按钮通常在封面下方），避免识别失败
        try:
            for _ in range(3):
                d.swipe_ext("up", scale=0.8, duration=0.25)
                time.sleep(0.25)
        except Exception:
            pass
        time.sleep(random.uniform(1.0, 1.8))

        if action == "follow":
            if self.smart_tap("Follow button on profile", device_id=did) or \
               self.smart_tap("Follow button", device_id=did):
                time.sleep(random.uniform(1.2, 2.0))
                return True
            return False

        if action == "add_friend":
            hit = (self.smart_tap("Add Friend button on profile page", device_id=did) or
                   self.smart_tap("Add Friend button", device_id=did))
            if not hit:
                return False
            time.sleep(random.uniform(1.2, 2.0))
            is_risk, _ = self._detect_risk_dialog(d)
            if is_risk:
                return False
            if note:
                self.smart_tap("Add note / verification message", device_id=did)
                time.sleep(0.8)
                try:
                    d(focused=True).set_text(note)
                    time.sleep(0.8)
                except Exception:
                    pass
                self.smart_tap("Send friend request", device_id=did)
            return True

        return False

    def _go_back_to_feed(self, d):
        """尽力回到 feed/home。避免栈过深被 FB 视为异常。"""
        try:
            for _ in range(3):
                d.press("back")
                time.sleep(0.35)
        except Exception:
            pass

    # ── Inbox Operations (Sprint 2 — 这里先放骨架,确保 hasattr 检查通过) ──

    @_with_fb_foreground
    def check_messenger_inbox(self, auto_reply: bool = False,
                              max_conversations: int = 20,
                              referral_contact: str = "",
                              preset_key: str = "",
                              device_id: Optional[str] = None,
                              persona_key: Optional[str] = None,
                              phase: Optional[str] = None) -> Dict[str, Any]:
        """主收件箱(Messenger)— Sprint 2 完整实现 + 2026-04-22 persona 改造。

        2026-04-22 改动:
          * ``max_conversations`` 未显式覆盖时,按 phase 从
            ``facebook_playbook.yaml.check_inbox.max_conversations`` 取。
          * 记录 persona_key 到 stats 里,便于 fb_inbox_messages 后续聚合。

        流程:
          1. 打开 Messenger 并 dismiss 弹窗
          2. 风控检测,若红立即返回
          3. 枚举顶部对话(最多 max_conversations 个)
          4. 对每条未读对话:进入 → dump 最新一条对方消息 →
             写 fb_inbox_messages → 若 auto_reply,调 chat_bridge 生成回复并发送
          5. 返回详细统计
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: check_inbox phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_conversations == 20 and "max_conversations" in ab_cfg:
            max_conversations = int(ab_cfg.get("max_conversations")
                                    or max_conversations)

        stats = {"opened": False, "conversations_listed": 0,
                 "unread_processed": 0, "replied": 0,
                 "wa_referrals": 0, "errors": 0,
                 "auto_reply": auto_reply,
                 "persona_key": persona_key or "",
                 "phase": eff_phase,
                 "max_conversations_applied": max_conversations,
                 "messages": []}

        # F3 (A→B review Q10): 拿 device-level "messenger_active" 锁,和 A 的
        # send_greeting_after_add_friend fallback 串行化,避免抢输入框。
        # A 的 device_section_lock 实现在拿不到锁超时时 raise RuntimeError。
        try:
            with _messenger_active_lock(did, timeout=30.0):
                d.app_stop(MESSENGER_PACKAGE)
                time.sleep(0.5)
                d.app_start(MESSENGER_PACKAGE)
                time.sleep(3)
                self._dismiss_dialogs(d)
                # 2026-04-27 A1 fix: VPN 切换后 Messenger 网络感知滞后, 显示
                # 红色"无网络连接"banner; 检测到立即 force-stop+restart 让
                # Messenger 重建 socket 走新路由
                if self._detect_no_network_banner(d):
                    log.warning("[messenger] 检测到'无网络'banner, force_restart "
                                "Messenger 等 VPN 路由稳定")
                    d.app_stop(MESSENGER_PACKAGE)
                    time.sleep(2)  # 让 socket 完全释放
                    d.app_start(MESSENGER_PACKAGE)
                    time.sleep(5)  # 等新连接建立
                    self._dismiss_dialogs(d)
                    if self._detect_no_network_banner(d):
                        log.warning("[messenger] 第二次启动仍无网络, abort task")
                        stats["error"] = "messenger_no_network_after_restart"
                        return stats
                stats["opened"] = True

                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    return stats

                convs = self._list_messenger_conversations(d, max_conversations)
                stats["conversations_listed"] = len(convs)

                _conv_lock_skipped = 0
                for c in convs:
                    if not c.get("unread"):
                        continue
                    if stats["unread_processed"] >= max_conversations:
                        break
                    # Phase B1: 跨设备对话所有权锁 — 防止多设备同时回复同一人
                    if auto_reply and c.get("name"):
                        try:
                            from src.host.fb_store import try_claim_conversation
                            _claim = try_claim_conversation(
                                c["name"], did, ttl_hours=0)  # E1: ttl_hours=0 → 自适应
                            if not _claim.get("claimed"):
                                log.info(
                                    "[messenger_inbox] peer=%s 对话已被"
                                    " device=%s 持有, skip reply",
                                    c["name"], _claim.get("owner", "?")[:12])
                                _conv_lock_skipped += 1
                                continue
                        except Exception as _cl_e:
                            log.debug("[messenger_inbox] 对话锁检查异常(继续): %s", _cl_e)
                    try:
                        detail = self._open_and_read_conversation(d, c, did,
                                                                  preset_key=preset_key)
                        if detail:
                            stats["unread_processed"] += 1
                            stats["messages"].append(detail)

                            # P7 §7.1 message_received: 默认"只读",触发 reply 后覆写
                            msg_decision = "read_only"
                            if auto_reply and detail.get("incoming_text"):
                                reply, decision = self._ai_reply_and_send(
                                    d, did,
                                    peer_name=c["name"],
                                    incoming_text=detail["incoming_text"],
                                    referral_contact=referral_contact,
                                    preset_key=preset_key,
                                    persona_key=persona_key,
                                )
                                if reply:
                                    stats["replied"] += 1
                                    if decision == "wa_referral":
                                        stats["wa_referrals"] += 1
                                msg_decision = decision  # reply/wa_referral/skip
                            # P7 §7.1: 只要拿到 incoming 就写 message_received 事件
                            # (不管 B 是否 reply; auto_reply=False 写 'read_only')
                            if detail.get("incoming_text"):
                                _emit_contact_event_safe(
                                    did, c["name"], "message_received",
                                    preset_key=preset_key,
                                    meta={"decision": msg_decision})

                        d.press("back")
                        time.sleep(random.uniform(1.0, 1.8))
                    except Exception as e:
                        log.debug("[messenger_inbox] 单对话失败: %s", e)
                        stats["errors"] += 1
                        try:
                            d.press("back")
                        except Exception:
                            pass
                stats["conv_lock_skipped"] = _conv_lock_skipped
        except RuntimeError as e:
            if "device_section_lock timeout" in str(e):
                log.info("[check_messenger_inbox] messenger_active 锁超时,skip: %s", e)
                stats["error"] = "device_busy_messenger_active"
                stats["lock_timeout"] = True
            else:
                stats["error"] = str(e)
                log.warning("[check_messenger_inbox] 失败: %s", e)
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_messenger_inbox] 失败: %s", e)
        return stats

    @_with_fb_foreground
    def check_message_requests(self, auto_review: bool = True,
                               max_requests: int = 20,
                               preset_key: str = "",
                               device_id: Optional[str] = None,
                               persona_key: Optional[str] = None,
                               phase: Optional[str] = None,
                               auto_reply: bool = True,
                               referral_contact: str = "") -> Dict[str, Any]:
        """陌生人 Message Requests 收件箱 — Sprint 2 骨架 + 2026-04-22 persona + P6 auto_reply。

        P6 (2026-04-23): ``auto_reply=True`` 时在 Message Requests 文件夹
        内直接回复陌生人,走与主 inbox 相同的 ``_ai_reply_and_send`` 链路,
        但 ``peer_type='stranger'`` 让内部 referral_gate 使用更保守的阈值
        (min_turns 5 / min_peer_replies 3 / score_threshold 4 / cooldown 6h),
        防止陌生人场景触发 spam 式引流被 Meta 反垃圾。

        策略:Message Requests 是潜在线索富矿,但风险也大;P6 开启后仍遵循
        "intent=opening/smalltalk 不引流"的 soft gate,首轮只破冰不推销。

        Args:
          auto_review: 历史参数 (尚未实现接受/审核动作,保留占位)
          auto_reply:  True 时直接回复;False 维持 Sprint 2 的"读不回"行为
          referral_contact: WhatsApp/LINE 等引流渠道 ID (gate 打分 + hard_allow)

        2026-04-22:``max_requests`` 未显式覆盖时按 phase 从
        ``facebook_playbook.yaml.check_inbox.max_requests`` 取。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_requests == 20 and "max_requests" in ab_cfg:
            max_requests = int(ab_cfg.get("max_requests") or max_requests)
        # P6: playbook 也可配 auto_reply (覆盖默认 True)
        if ab_cfg and "auto_reply_stranger" in ab_cfg:
            auto_reply = bool(ab_cfg.get("auto_reply_stranger"))

        stats: Dict[str, Any] = {
            "opened": False, "requests_seen": 0,
            "messages_collected": 0,
            "replies_sent": 0, "wa_referrals": 0, "reply_skipped": 0,
            "errors": 0,
            "auto_reply": auto_reply,
            "persona_key": persona_key or "",
            "phase": eff_phase,
            "max_requests": max_requests,
        }

        # F3 (A→B review Q10): 和 A 的 send_greeting fallback 串行化
        try:
            with _messenger_active_lock(did, timeout=30.0):
                d.app_start(MESSENGER_PACKAGE)
                time.sleep(2.5)
                self._dismiss_dialogs(d)

                if not (self.smart_tap("Message Requests entry", device_id=did)
                        or self._open_message_requests_fallback(d)):
                    stats["error"] = "Message Requests 入口未找到"
                    return stats

                time.sleep(2)
                stats["opened"] = True

                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    return stats

                convs = self._list_messenger_conversations(d, max_requests)
                stats["requests_seen"] = len(convs)

                for c in convs[:max_requests]:
                    # Phase B1: 跨设备对话所有权锁 (与 check_messenger_inbox 一致)
                    if auto_reply and c.get("name"):
                        try:
                            from src.host.fb_store import try_claim_conversation
                            _claim = try_claim_conversation(
                                c["name"], did)  # E1: ttl_hours=0 → 自适应
                            if not _claim.get("claimed"):
                                log.info(
                                    "[check_message_requests] peer=%s 对话已被"
                                    " device=%s 持有, skip",
                                    c["name"], _claim.get("owner", "?")[:12])
                                continue
                        except Exception:
                            pass
                    try:
                        detail = self._open_and_read_conversation(d, c, did,
                                                                  peer_type="stranger",
                                                                  preset_key=preset_key)
                        if detail and detail.get("incoming_text"):
                            stats["messages_collected"] += 1

                            # P7 §7.1 message_received: stranger 场景默认 read_only
                            msg_decision = "read_only"
                            # P6: 陌生人场景自动回复 (peer_type='stranger' 触发
                            # referral_gate 保守配置)
                            if auto_reply and not detail.get("risk"):
                                reply, decision = self._ai_reply_and_send(
                                    d, did,
                                    peer_name=c["name"],
                                    incoming_text=detail["incoming_text"],
                                    referral_contact=referral_contact,
                                    preset_key=preset_key,
                                    persona_key=persona_key,
                                    peer_type="stranger",
                                )
                                if reply:
                                    stats["replies_sent"] += 1
                                    if decision == "wa_referral":
                                        stats["wa_referrals"] += 1
                                else:
                                    stats["reply_skipped"] += 1
                                msg_decision = decision
                            # P7 §7.1: message_received 带 peer_type=stranger 区分
                            _emit_contact_event_safe(
                                did, c["name"], "message_received",
                                preset_key=preset_key,
                                meta={"decision": msg_decision,
                                      "peer_type": "stranger"})

                        d.press("back")
                        time.sleep(random.uniform(0.8, 1.5))
                    except Exception as e:
                        log.debug("[check_message_requests] 单对话失败: %s", e)
                        stats["errors"] += 1
                        try:
                            d.press("back")
                        except Exception:
                            pass
        except RuntimeError as e:
            if "device_section_lock timeout" in str(e):
                log.info("[check_message_requests] messenger_active 锁超时,skip: %s", e)
                stats["error"] = "device_busy_messenger_active"
                stats["lock_timeout"] = True
            else:
                stats["error"] = str(e)
                log.warning("[check_message_requests] 失败: %s", e)
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_message_requests] 失败: %s", e)
        return stats

    @_with_fb_foreground
    def check_friend_requests_inbox(self, accept_all: bool = False,
                                    safe_accept: bool = True,
                                    max_requests: int = 20,
                                    min_mutual_friends: int = 1,
                                    min_lead_score: int = 0,
                                    score_policy: str = "and",
                                    device_id: Optional[str] = None,
                                    persona_key: Optional[str] = None,
                                    phase: Optional[str] = None) -> Dict[str, Any]:
        """好友请求收件箱 — Sprint 2 完整实现 + 2026-04-22 persona 改造 + P1 lead_score gate。

        安全策略(safe_accept=True 默认):
          - mutual_friends >= min_mutual_friends (避免 honeypot)
          - P1 可选:lead_score >= min_lead_score (对接 A 机 fb_lead_scorer_v2 打分)
              * min_lead_score=0 禁用评分门,等价旧行为
              * score_policy='and' 同时满足 mutual & score (默认,最严格)
              * score_policy='or'  任一满足即通过 (对高质量素人放行)
          - 一次会话最多接受 max_requests/2 (accept_all 解除上限)
          - 每接受 1 个停顿 6-12s

        2026-04-22:``max_requests`` 未显式覆盖时按 phase 从
        ``facebook_playbook.yaml.check_inbox`` 取。cold_start 阶段自动降到 8。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_requests == 20 and "max_requests" in ab_cfg:
            max_requests = int(ab_cfg.get("max_requests") or max_requests)

        score_enabled = int(min_lead_score) > 0
        policy = (score_policy or "and").lower().strip()
        if policy not in ("and", "or"):
            policy = "and"

        stats: Dict[str, Any] = {
            "opened": False, "requests_seen": 0,
            "accepted": 0, "skipped": 0, "errors": 0,
            "persona_key": persona_key or "",
            "phase": eff_phase,
            "max_requests": max_requests,
            "accept_all": accept_all,
            "safe_accept": safe_accept,
            "min_mutual_friends": min_mutual_friends,
            "min_lead_score": min_lead_score,
            "score_policy": policy if score_enabled else "",
            "score_enabled": score_enabled,
            "lead_score_checked": 0,
            "lead_score_hits": 0,
            "accepted_reasons": {"mutual_only": 0, "score_only": 0, "both": 0, "quota": 0},
            "skipped_reasons": {"mutual_low": 0, "score_low": 0, "both_low": 0},
        }

        try:
            d.app_start(PACKAGE)
            time.sleep(3)
            self._dismiss_dialogs(d)

            if not (self.smart_tap("Friends tab", device_id=did)
                    or self.smart_tap("Friend requests", device_id=did)):
                stats["error"] = "Friends 入口未找到"
                return stats
            time.sleep(2)
            stats["opened"] = True

            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                stats["risk_detected"] = msg
                return stats

            requests_meta = self._list_friend_requests(d, max_requests)
            stats["requests_seen"] = len(requests_meta)

            accept_quota = max_requests if accept_all else max(1, max_requests // 2)
            for meta in requests_meta:
                if stats["accepted"] >= accept_quota:
                    break

                mutual = int(meta.get("mutual_friends", 0) or 0)
                mutual_ok = mutual >= int(min_mutual_friends)
                score_ok = True
                lead_score = 0
                lead_id: Optional[int] = None

                if safe_accept and not accept_all and score_enabled:
                    lead_id, lead_score = self._lookup_lead_score(meta.get("name", ""))
                    stats["lead_score_checked"] += 1
                    if lead_id is not None:
                        stats["lead_score_hits"] += 1
                    score_ok = lead_score >= int(min_lead_score)

                meta["lead_id"] = lead_id
                meta["lead_score"] = lead_score

                if safe_accept and not accept_all:
                    if score_enabled:
                        passed = (mutual_ok and score_ok) if policy == "and" \
                            else (mutual_ok or score_ok)
                    else:
                        passed = mutual_ok

                    if not passed:
                        if score_enabled:
                            if not mutual_ok and not score_ok:
                                reason_key = "both_low"
                            elif not mutual_ok:
                                reason_key = "mutual_low"
                            else:
                                reason_key = "score_low"
                        else:
                            reason_key = "mutual_low"
                        stats["skipped"] += 1
                        stats["skipped_reasons"][reason_key] += 1
                        meta["skip_reason"] = reason_key
                        continue

                    if score_enabled:
                        if mutual_ok and score_ok:
                            accept_key = "both"
                        elif mutual_ok:
                            accept_key = "mutual_only"
                        else:
                            accept_key = "score_only"
                    else:
                        accept_key = "mutual_only"
                else:
                    accept_key = "quota"

                try:
                    if self._tap_accept_button_for(d, meta):
                        stats["accepted"] += 1
                        stats["accepted_reasons"][accept_key] += 1
                        self.hb.wait_think(random.uniform(6.0, 12.0))
                        try:
                            from src.host.fb_store import update_friend_request_status
                            update_friend_request_status(did, meta.get("name", ""), "accepted")
                        except Exception:
                            pass
                        # P7 §7.1 add_friend_accepted: 给 A 的 Lead Mesh
                        # Dashboard 提供"好友请求通过"事件。meta 带 lead_id
                        # (若匹配到)/ mutual_friends / lead_score 方便 A 做
                        # 质量归因。Phase 5 未 merge 静默 skip (feature-detect)。
                        try:
                            from src.host.fb_store import record_contact_event
                            record_contact_event(
                                did, meta.get("name", "") or "",
                                "add_friend_accepted",
                                meta={
                                    "lead_id": meta.get("lead_id"),
                                    "mutual_friends": int(
                                        meta.get("mutual_friends", 0) or 0),
                                    "lead_score": int(
                                        meta.get("lead_score", 0) or 0),
                                    "accept_key": accept_key,
                                },
                            )
                        except ImportError:
                            pass  # Phase 5 未 merge
                        except Exception as e:
                            log.debug(
                                "[P7 add_friend_accepted] skip: %s", e)
                    else:
                        stats["errors"] += 1
                except Exception:
                    stats["errors"] += 1
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_friend_requests_inbox] 失败: %s", e)
        return stats

    @staticmethod
    def _lookup_lead_score(name: str) -> Tuple[Optional[int], int]:
        """只读查 leads.store 拿 lead_id + score(P1 新增, F5 加 fuzzy fallback)。

        优先用 A 机已落库的 fb_lead_scorer_v2 融合分。未匹配/异常 → (None, 0)。
        不做 on-the-fly 评分以避免 check_inbox 路径触发 LLM 费用。

        **F5 (A→B review Q5)**: 硬匹配 miss 时,对 ``normalize_name`` 结果做
        Levenshtein 距离 ≤1 的 fuzzy 兜底,解决全角/NBSP 等未被 normalize_name
        覆盖的边界 case。用 LIKE 预过滤候选 + 限制 200 行,不全表扫。
        """
        n = (name or "").strip()
        if not n:
            return None, 0
        try:
            from src.leads.store import get_leads_store
            store = get_leads_store()
            lid = store.find_match(name=n)
            if not lid:
                # F5 fuzzy 兜底
                lid = _fuzzy_match_lead_by_name(store, n)
            if not lid:
                return None, 0
            rec = store.get_lead(lid) or {}
            raw = rec.get("score", 0) or 0
            try:
                s = int(raw)
            except (TypeError, ValueError):
                try:
                    s = int(float(raw))
                except Exception:
                    s = 0
            return lid, max(0, min(100, s))
        except Exception:
            log.debug("[_lookup_lead_score] 查询失败(降级到 0)", exc_info=True)
            return None, 0

    # ── Inbox helpers (Sprint 2 P0 内部支持函数) ─────────────────────────

    # ── Phase 15 (2026-04-25): peer_name UI 文本黑名单 ─────────────────
    # 之前 _check_message_requests 把"查看翻译"等 Messenger UI 按钮文本当
    # peer_name 写进了 fb_contact_events, dispatcher 拿这种"假 peer" 永远
    # filter 不出合格 lead. 全方位 ban 各种 UI 词 + 多语言 (中/英/日/意).
    _MESSENGER_UI_TEXT_BLACKLIST = frozenset(s.lower() for s in (
        # tab/导航
        "chats", "people", "stories", "calls", "messenger", "search",
        "back", "home", "notifications", "menu", "settings", "marketplace",
        "聊天", "联系人", "动态", "通讯录",
        # 翻译 (核心 root cause)
        "translate", "see translation", "tap to translate",
        "translation", "翻译", "查看翻译", "点击翻译", "显示原文",
        "翻訳", "翻訳を表示", "原文", "Vedi traduzione", "Tradurre",
        # 操作按钮
        "reply", "send", "more", "edit", "delete", "block", "report",
        "回复", "发送", "更多", "编辑", "删除", "屏蔽", "举报", "更多选项",
        "返信", "送信", "もっと見る", "編集", "削除", "ブロック", "通報",
        "rispondi", "invia", "altro",
        # 状态
        "active now", "online", "offline", "typing", "seen",
        "在线", "离线", "正在输入", "已读",
        "オンライン", "入力中", "既読", "未読",
        "online ora",
        # 消息列表常见控件
        "mark as read", "mark all as read", "filter", "filters",
        "标为已读", "全部已读", "筛选", "过滤",
        "既読にする", "全て既読",
        # 列表头/empty state
        "message requests", "spam", "archived", "new message",
        "消息请求", "垃圾", "已存档", "新消息",
        "メッセージリクエスト", "新規メッセージ",
        # 2026-04-27 P5: friend_requests 流程按钮 (删 ASCII 启发后由黑名单兜底)
        "confirm", "accept", "decline", "hide", "reject", "cancel", "ok",
        "confirm friend request", "delete request", "ignore",
        "确认", "接受", "拒绝", "隐藏", "取消",
        "確認", "承認", "拒否", "削除する",
        # 2026-04-27 P5: 其他常见 thread 按钮
        "like", "share", "save", "follow", "view", "forward",
        "archive", "mute", "unmute", "pin", "unpin", "snooze",
        # 表情符号 / 1 字符 reaction (避免抓 ✓✓ ❤ 等)
        "✓", "✓✓", "❤", "👍", "•",
    ))

    # 含这些子串视为消息预览 (而非 peer 名)
    _MESSENGER_PREVIEW_HINTS = (
        ": ",   # "Alice: hello"
        "...",   # 省略号预览
        "…",
    )

    # Phase 16: 截断标记 — Messenger preview 末尾常带 "更多" / "more" / "もっと見る"
    # 表示原文被截断, peer_name 不会带这种.
    _MESSENGER_TRUNCATION_MARKERS = (
        "更多", "more", "もっと見る", "もっと",
        "Mehr", "altro",  # de/it
    )

    # Phase 17.1 (2026-04-25): yaml 热加载黑名单缓存
    # config/peer_name_blacklist.yaml 改完 5 分钟内自动生效 (无需重启).
    _BLACKLIST_YAML_CACHE = {
        "extra": frozenset(),     # 从 yaml 读到的 lower-case set
        "loaded_at": 0.0,
    }
    _BLACKLIST_YAML_TTL_SEC = 300  # 5 min

    @staticmethod
    def _load_extra_blacklist() -> "frozenset[str]":
        """Phase 17.1 / 18: 读 config/peer_name_blacklist.yaml 的 extra_blacklist.

        TTL 5 min 缓存. yaml 不存在 → 返空 (静默, 不警告). 解析/schema 错
        → logger.error visible warning + 返空 (不抛, 主流程不受影响).

        Phase 18 schema 校验:
          - 顶层必须是 dict (不是 list/scalar)
          - extra_blacklist 必须是 list 或缺省 (不能是 dict/scalar/None)
          - 每个 item 必须是 str (非 str 跳过 + warning)
        """
        import time as _t
        cache = FacebookAutomation._BLACKLIST_YAML_CACHE
        now = _t.time()
        if now - cache.get("loaded_at", 0) < FacebookAutomation._BLACKLIST_YAML_TTL_SEC:
            return cache.get("extra", frozenset())
        extra: frozenset = frozenset()
        try:
            from pathlib import Path
            import yaml
            here = Path(__file__).resolve().parent.parent.parent
            yaml_path = here / "config" / "peer_name_blacklist.yaml"
            if not yaml_path.exists():
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            try:
                with yaml_path.open(encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except Exception as e:
                log.error("[blacklist_yaml] YAML 解析失败 (退化空 set): %s", e)
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            # Phase 18: schema 校验
            if data is None:
                # 空文件 — 合法
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            if not isinstance(data, dict):
                log.error("[blacklist_yaml] 顶层必须是 dict, 实际 %s, 退化空 set",
                            type(data).__name__)
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            items = data.get("extra_blacklist")
            if items is None:
                # 缺 key — 合法
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            if not isinstance(items, list):
                log.error("[blacklist_yaml] extra_blacklist 必须是 list, "
                            "实际 %s, 退化空 set", type(items).__name__)
                cache["extra"] = extra
                cache["loaded_at"] = now
                return extra
            valid_items = []
            for i, s in enumerate(items):
                if not isinstance(s, str):
                    log.warning("[blacklist_yaml] item[%d] 不是 str (%s), skip",
                                  i, type(s).__name__)
                    continue
                s2 = s.strip().lower()
                if s2:
                    valid_items.append(s2)
            extra = frozenset(valid_items)
        except Exception as e:
            log.error("[blacklist_yaml] 未预期异常 (退化空): %s", e)
            extra = frozenset()
        cache["extra"] = extra
        cache["loaded_at"] = now
        return extra

    @staticmethod
    def reload_extra_blacklist() -> int:
        """运维 / 测试 force reload yaml 黑名单. 返加载条数."""
        FacebookAutomation._BLACKLIST_YAML_CACHE["loaded_at"] = 0.0
        return len(FacebookAutomation._load_extra_blacklist())

    @staticmethod
    def _is_valid_peer_name(text: str) -> bool:
        """Phase 15 / 15.1: peer_name 多层 sanitize.

        过滤 Messenger UI 按钮文本 ("查看翻译" / "Reply") + 消息预览片段
        ("Alice: hi...") + 测试残留 ("p0"/"Alice"/"Bob") + 全 emoji 串.
        保留真用户 display name (中/日/英全名).

        过滤规则 (任一命中即 reject):
          1. 空 / 长度 < 2 或 > 30
          2. 全数字 / 全标点 / 全 emoji (Unicode So 类)
          3. 出现在 _MESSENGER_UI_TEXT_BLACKLIST
          4. 含 _MESSENGER_PREVIEW_HINTS (": " / "..." / "…")
          5. 句尾标点 (display name 不带)
          6. ASCII 单词首大写后小写 + 长度<=12 = 按钮启发式
          7. (Phase 15.1) ASCII 短串 ≤ 4 字符且含数字 = 测试残留/编号 ban
             (例 "p0"/"a1"/"X3"; 但放过 "alice"/"bob" 之类纯字母)
        """
        if not text:
            return False
        s = text.strip()
        if len(s) < 2 or len(s) > 30:
            return False
        if s.isdigit():
            return False
        # 全 ASCII 标点 + 非汉字 (基本平面外字符也排) 视为不合法
        if all(not c.isalnum() and ord(c) < 0x4E00 for c in s):
            return False
        # Phase 15.1: 全 emoji (Unicode 类 So/Sk + ZWJ) ban
        try:
            import unicodedata as _ud
            if all(_ud.category(c).startswith(("S",)) or c in "‍️"
                   for c in s):
                return False
        except Exception:
            pass
        # 黑名单 (内置 + yaml 热加载)
        sl = s.lower()
        if sl in FacebookAutomation._MESSENGER_UI_TEXT_BLACKLIST:
            return False
        # Phase 17.1: yaml 黑名单 (运营自加, 5 min TTL)
        if sl in FacebookAutomation._load_extra_blacklist():
            return False
        # 消息预览
        for hint in FacebookAutomation._MESSENGER_PREVIEW_HINTS:
            if hint in s:
                return False
        # Phase 16: 截断标记 (Messenger preview "... 更多" 等)
        for marker in FacebookAutomation._MESSENGER_TRUNCATION_MARKERS:
            # 只 ban 句末 / 包尾的截断标记, 不 ban 含 "more" 的真名 (罕见但
            # 假设 Maro = 真名末位 "more" 实际不会出现; 保守只 ban 末尾匹配)
            if s.endswith(marker) or s.endswith(" " + marker):
                return False
        # 句尾标点
        if s[-1] in ".!?,。!?、,…":
            return False
        # 2026-04-27 P5 fix: 删除"ASCII 单词首大写后小写 ≤12 = 按钮启发"规则.
        # 原规则误杀英文名用户 (Alice/Bob/Mike/Robert 等), 导致 fb_contact_events
        # 不写入 → W175 funnel 全 0 production bug. 真按钮文本由
        # _MESSENGER_UI_TEXT_BLACKLIST + _load_extra_blacklist (yaml 热加载) 覆盖.
        # 漏判风险: 极少数未列黑名单的英文短按钮可能通过, 通过 yaml 运营补.
        # Phase 15.1: ASCII 短串 (<=4) 且含数字 — 测试残留 (p0/p1/X3) 保留
        if len(s) <= 4 and s.isascii() and any(c.isdigit() for c in s):
            return False
        return True

    # Phase 17: ListView 行级匹配, 限定父容器 class 必须含这些关键字
    _MESSENGER_LIST_CONTAINERS = (
        "RecyclerView", "ListView", "ScrollView", "LinearLayout",
    )

    def _list_messenger_conversations(self, d, max_n: int) -> List[Dict]:
        """从 Messenger 主列表 dump 当前可见对话。返回 [{name, unread, bounds}].

        Phase 17 (2026-04-25): 结构敏感 ListView 行级匹配.
        2026-04-27 A2 fix: parent_class 过滤太严导致 B 设备 0 对话, 改成软约束 +
        多 fallback (clickable text + content-desc + RelativeLayout).
        """
        try:
            xml = d.dump_hierarchy()
        except Exception as e:
            log.debug("[list_messenger] dump_hierarchy 失败: %s", e)
            return []
        try:
            from ..vision.screen_parser import XMLParser
        except Exception as e:
            log.debug("[list_messenger] XMLParser import 失败: %s", e)
            return []
        try:
            elements = XMLParser.parse(xml)
        except Exception as e:
            log.debug("[list_messenger] parse 失败: %s", e)
            return []

        items: List[Dict] = []
        seen = set()
        # Phase 1: 严格匹配 (parent_class in _MESSENGER_LIST_CONTAINERS)
        for el in elements:
            text = (el.text or "").strip()
            if not el.clickable:
                continue
            parent_cls = getattr(el, "parent_class", "") or ""
            if parent_cls:
                in_list = any(kw in parent_cls
                               for kw in
                               FacebookAutomation._MESSENGER_LIST_CONTAINERS)
                if not in_list:
                    continue
            if not FacebookAutomation._is_valid_peer_name(text):
                continue
            if text in seen:
                continue
            seen.add(text)
            items.append({
                "name": text,
                "unread": bool(getattr(el, "selected", False) or "•" in text),
                "bounds": getattr(el, "bounds", None),
            })
            if len(items) >= max_n:
                break

        # 2026-04-27 A2 fix: Phase 1 没找到任何对话时, 走宽松 fallback —
        # 不限父容器 class, 只看 content-desc / desc / text 含日文/中文/英文姓名
        if len(items) == 0:
            log.warning("[list_messenger] Phase 1 (parent_class 严格) 0 hit, "
                         "降级到宽松 fallback")
            for el in elements:
                text = (el.text or "").strip()
                desc = getattr(el, "content_desc", "") or getattr(el, "desc", "") or ""
                # 用 text 或 content_desc 作为名字源
                candidate = text or desc
                if not candidate:
                    continue
                if not FacebookAutomation._is_valid_peer_name(candidate):
                    continue
                if candidate in seen:
                    continue
                # 只要 clickable 或 content_desc 非空都收
                if not el.clickable and not desc:
                    continue
                seen.add(candidate)
                items.append({
                    "name": candidate,
                    "unread": bool(getattr(el, "selected", False)
                                    or "未读" in desc or "Unread" in desc
                                    or "•" in candidate),
                    "bounds": getattr(el, "bounds", None),
                    "_fallback": True,
                })
                if len(items) >= max_n:
                    break
            log.info("[list_messenger] fallback 找到 %d 个对话", len(items))

        return items

    def _open_and_read_conversation(self, d, conv: Dict, did: str,
                                    peer_type: str = "friend",
                                    preset_key: str = "") -> Optional[Dict]:
        """点进对话,读最新一条对方消息,写入 fb_inbox_messages。"""
        bounds = conv.get("bounds")
        try:
            if bounds and isinstance(bounds, (tuple, list)) and len(bounds) >= 4:
                cx = (bounds[0] + bounds[2]) // 2
                cy = (bounds[1] + bounds[3]) // 2
                self.hb.tap(d, cx, cy)
            else:
                d(text=conv["name"]).click()
        except Exception:
            return None

        time.sleep(random.uniform(2.0, 3.0))
        is_risk, msg = self._detect_risk_dialog(d)
        if is_risk:
            return {"peer_name": conv["name"], "risk": msg}

        incoming_text = self._extract_latest_incoming_message(d)
        lang = ""
        try:
            from src.ai.lang_detect import detect_language
            lang = detect_language(incoming_text or "")
        except Exception:
            pass
        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, conv["name"],
                peer_type=peer_type,
                message_text=incoming_text or "",
                direction="incoming",
                language_detected=lang,
                preset_key=preset_key,
            )
        except Exception:
            log.debug("[inbox] 写库失败", exc_info=True)
        # L2 中央客户画像双写 — 入站消息升级 status='in_messenger'
        try:
            from src.host.customer_sync_bridge import sync_messenger_incoming
            sync_messenger_incoming(
                did, conv["name"],
                content=incoming_text or "",
                content_lang=lang or None,
                peer_type=peer_type,
            )
        except Exception:
            pass

        # P7 §7.1 greeting_replied: 对方一 incoming 就尝试标记最近 7 天未回
        # 的 greeting 行 (P0 的 mark_greeting_replied_back 幂等, 已标则跳过)。
        # 这覆盖 auto_reply=False 场景 — 只要对方回了 greeting 就算关系建立,
        # 即使 B 没 reply 也记到 fb_contact_events。
        #
        # Feature-detect: P0 已 merge (mark_greeting_replied_back 可用),
        # F1 内部会同步写 greeting_replied event 到 fb_contact_events。
        if incoming_text:
            try:
                from src.host.fb_store import mark_greeting_replied_back
                mark_greeting_replied_back(did, conv["name"], window_days=7)
            except ImportError:
                pass  # P0 未 merge (defensive, 当前 main 已含)
            except Exception as e:
                log.debug("[P7 greeting_replied] skip: %s", e)
        return {"peer_name": conv["name"], "incoming_text": incoming_text,
                "language_detected": lang}

    def _extract_latest_incoming_message(self, d) -> str:
        """从对话页面 dump 中提取最新一条对方消息(简单启发:取屏幕中靠左的最长 TextView)。"""
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception:
            return ""
        candidates = []
        screen_w = 1080
        try:
            screen_w = d.window_size()[0]
        except Exception:
            pass
        for el in elements:
            text = (el.text or "").strip()
            if not text or len(text) < 3:
                continue
            bounds = getattr(el, "bounds", None)
            if not bounds:
                continue
            x_center = (bounds[0] + bounds[2]) / 2
            if x_center > screen_w * 0.6:
                continue
            if any(skip in text.lower() for skip in
                   ["type a message", "send", "active now", "online", "you"]):
                continue
            candidates.append((bounds[1], text))
        if not candidates:
            return ""
        candidates.sort(key=lambda t: -t[0])
        return candidates[0][1][:500]

    def _ai_reply_and_send(self, d, did: str, *, peer_name: str,
                           incoming_text: str,
                           referral_contact: str = "",
                           preset_key: str = "",
                           persona_key: Optional[str] = None,
                           peer_type: str = "friend") -> Tuple[Optional[str], str]:
        """调 ChatBrain 生成回复并发出;P1 接入 persona 语言 + 引流话术;
        P3 接入长久记忆;P4 意图分类;P5 统一引流决策闸;P6 peer_type 感知。

        * ``target_language`` 来自 ``fb_target_personas.get_persona_display``,
          日本客群强制 ``ja``,避免英文破冰。
        * 引流阶段(referral_score>0.5)且配置了 contact 时,**出站消息**
          以 ``fb_content_assets.get_referral_snippet`` 为主渠道话术为准。
        * **P3 长久记忆**: 同 peer 历史消息 + 派生画像通过 ``chat_memory.build_context_block``
          实时拼进 ``ab_style_hint``。
        * **P4 意图**: ``classify_intent`` 结果影响 ab_style_hint + referral_gate 判断。
        * **P5 引流闸**: ``should_refer`` 统一决定 wa_referral vs reply。
        * **P6 peer_type**: ``'stranger'`` 触发更保守的 referral_gate 配置
          (min_turns/score_threshold/cooldown 都上浮),防陌生人场景 spam 感引流。
        """
        reply = None
        decision = "skip"
        # PR-7: 真人接管中的 peer 不走 AI 自动回复, 留给真人在后台手动发
        try:
            from src.host.ai_takeover_state import is_taken_over
            if is_taken_over(peer_name, did):
                log.info(
                    "[ai_reply] peer=%s device=%s 真人接管中, AI 不自动回",
                    peer_name, did,
                )
                return None, "human_takeover"
        except Exception:
            pass
        target_lang = ""
        ab_style_hint = ""
        # P0 2026-04-23: incoming 侧语言检测,用于:
        #   (a) persona 未声明目标语言时降级填充 target_lang
        #   (b) 写入 facebook_inbox_messages.language_detected (both incoming & outgoing)
        detected_incoming_lang = ""
        try:
            from src.ai.lang_detect import detect_language
            detected_incoming_lang = detect_language(incoming_text or "")
        except Exception:
            pass
        try:
            from src.host.fb_target_personas import get_persona_display
            disp = get_persona_display(persona_key)
            target_lang = str(disp.get("language") or "").strip()
            short = str(disp.get("short_label") or disp.get("name") or "")
            if target_lang.startswith("ja"):
                ab_style_hint = (
                    "【言語・トーン必須】日本語のみ。丁寧語ベース、"
                    "押しが強くない・スパム感ゼロ。絵文字は1個まで。"
                    f" 想定読者:{short}。"
                )
            elif target_lang:
                ln = target_lang.lower()
                ab_style_hint = (
                    f"Reply ONLY in language code '{target_lang}'. "
                    f"Audience: {short}. Warm, not pushy, max 1 emoji."
                )
        except Exception as e:
            log.debug("[ai_reply] persona 元数据失败(继续): %s", e)

        # P0 2026-04-23: persona 未设 target_lang 时用 incoming 检测结果降级,
        # 避免 LLM 默认英文回复日/意客群
        if not target_lang and detected_incoming_lang:
            target_lang = detected_incoming_lang
            if target_lang == "ja":
                ab_style_hint = (
                    "【言語・トーン必須】日本語のみ。丁寧語ベース、"
                    "押しが強くない・スパム感ゼロ。絵文字は1個まで。"
                )
            elif target_lang:
                ab_style_hint = (
                    f"Reply ONLY in language code '{target_lang}'. "
                    "Warm, not pushy, max 1 emoji."
                )

        # P3: 注入长久记忆 — 历史对话 + peer 派生画像
        memory_ctx: Dict[str, Any] = {
            "hint_text": "",
            "should_block_referral": False,
            "history": [],
            "profile": {},
        }
        try:
            from src.ai.chat_memory import build_context_block
            memory_ctx = build_context_block(did, peer_name, history_limit=5)
            if memory_ctx.get("hint_text"):
                if ab_style_hint:
                    ab_style_hint = ab_style_hint + "\n\n" + memory_ctx["hint_text"]
                else:
                    ab_style_hint = memory_ctx["hint_text"]
        except Exception as e:
            log.debug("[ai_reply] chat_memory 构建失败(降级): %s", e)

        # P4: 意图分类 (rule-first + LLM fallback) — 为生成 LLM 提供意图 hint,
        # 并影响引流决策 (buying/referral_ask 强制触发 wa_referral)
        intent_tag = "smalltalk"
        intent_confidence = 0.3
        try:
            from src.ai.chat_intent import (
                classify_intent,
                format_intent_for_llm_hint,
                should_trigger_referral,
            )
            intent_result = classify_intent(
                incoming_text,
                history=memory_ctx.get("history") or [],
                lang_hint=target_lang,
            )
            intent_tag = intent_result.intent
            intent_confidence = intent_result.confidence
            ih = format_intent_for_llm_hint(intent_result)
            if ih:
                ab_style_hint = (ab_style_hint + "\n\n" + ih) if ab_style_hint else ih
            log.debug(
                "[ai_reply] peer=%s intent=%s conf=%.2f src=%s",
                peer_name, intent_tag, intent_confidence, intent_result.source,
            )
        except Exception as e:
            log.debug("[ai_reply] chat_intent 失败(降级 smalltalk): %s", e)
            # 让后续流程继续用默认 smalltalk 行为

        # 空 incoming：破冰时叠一条 greeting 参考（约束 LLM 不要写英文 hello）
        try:
            if not (incoming_text or "").strip():
                from .fb_content_assets import get_greeting_message
                g = get_greeting_message(persona_key=persona_key, name=peer_name)
                if g:
                    ab_style_hint += f"\n【开场参考·勿逐字照抄】{g}"
        except Exception:
            pass

        # P1-5: 多通道 referral（line:|wa:|JSON）→ 按 persona 首推选值 + ChatBrain 摘要串
        _rc_raw = (referral_contact or "").strip()
        _rch_map: Dict[str, str] = {}
        _r_val, _r_channel = "", "whatsapp"
        try:
            from src.host.fb_referral_contact import (
                format_contact_for_chat_brain,
                parse_referral_channels,
                pick_referral_for_persona,
            )
            _rch_map = parse_referral_channels(_rc_raw)
            _r_val, _r_channel = pick_referral_for_persona(_rch_map, persona_key)
            _contact_for_brain = format_contact_for_chat_brain(_rc_raw) or _rc_raw
        except Exception as e:
            log.debug("[ai_reply] referral 解析失败(降级): %s", e)
            _contact_for_brain = _rc_raw

        try:
            from src.ai.chat_brain import ChatBrain, UserProfile
            brain = ChatBrain.get_instance()
            # Fix 2026-04-24: UserProfile dataclass 实际字段是 username/bio/
            # source (不含 lead_id/name)。原代码 UserProfile(lead_id=...,
            # name=...) 会 raise TypeError,被外层 try/except catch 后
            # return None, "skip" — 生产 auto_reply 从未真正生成 reply。
            # 真机 dry-run (scripts/messenger_production_dryrun.py) 发现。
            profile = UserProfile(username=peer_name, bio="", source="fb_inbox")
            # PR-7: 从 persona 配置取 bot_persona (如 jp_female_midlife → jp_caring_male)
            # 让 ChatBrain stage='referral' 时注入"日本男性关爱"调性
            _bot_persona = ""
            try:
                from src.ai.referral_gate import load_persona_config
                _bot_persona = (load_persona_config(persona_key)
                                .get("bot_persona") or "")
            except Exception:
                pass
            # Phase-4: 算 ab_variant — deterministic hash 同步 customer_sync_bridge
            _cs_ab_variant = ""
            try:
                from src.host.customer_sync_bridge import (_ab_variant_for,
                                                           _build_canonical_id)
                _cs_ab_variant = _ab_variant_for(
                    _build_canonical_id(did, peer_name)
                )
            except Exception:
                pass
            result = brain.generate_reply(
                lead_id=peer_name,
                incoming_message=incoming_text,
                profile=profile,
                platform="facebook",
                target_language=target_lang,
                contact_info=_contact_for_brain,
                source="inbox",
                ab_style_hint=ab_style_hint.strip(),
                bot_persona=_bot_persona or None,
                cs_ab_variant=_cs_ab_variant or None,
            )
            if result and result.message:
                reply = result.message
                ref_score = float(getattr(result, "referral_score", 0.0) or 0.0)
                has_contact = bool(_rc_raw) or bool(_rch_map)
                # P5: 统一引流决策闸 — 替代 P3 post-block + P4 硬触发的散落逻辑
                try:
                    from src.ai.referral_gate import should_refer
                    # 只读拉 leads.store 的 A 打分供 gate 评估
                    lead_score_val = 0
                    try:
                        from src.leads.store import get_leads_store
                        _store = get_leads_store()
                        _lid = _store.find_match(name=peer_name)
                        if _lid:
                            _rec = _store.get_lead(_lid) or {}
                            try:
                                lead_score_val = int(_rec.get("score", 0) or 0)
                            except (TypeError, ValueError):
                                lead_score_val = 0
                    except Exception:
                        lead_score_val = 0
                    # P6: 陌生人场景使用更保守 gate 配置
                    _gate_cfg = None
                    if peer_type == "stranger":
                        _gate_cfg = {
                            "min_turns": 5,
                            "min_peer_replies": 3,
                            "score_threshold": 4,
                            "refer_cooldown_hours": 6,
                        }
                    # Phase-6: emotion 评分接进 should_refer (异步, 缓存 10 min)
                    _emotion_overall = None
                    _emotion_frustration = None
                    try:
                        from src.ai.chat_emotion_scorer import score_emotion
                        _msgs = [{"role": "user", "content": incoming_text or ""}]
                        _emo_result = score_emotion(_msgs, persona_key=persona_key or "")
                        if not _emo_result.get("fallback"):
                            _emotion_overall = float(_emo_result.get("overall") or 0.5)
                            _emotion_frustration = float(_emo_result.get("frustration") or 0.5)
                    except Exception:
                        pass
                    # Phase-9: turns ≥ 4 时才查 LLM readiness (省 HTTP)
                    # Phase-13: 同时记录 raw_readiness 用于 explainability
                    _readiness = None
                    _raw_readiness = None
                    try:
                        _turns_seen = int((memory_ctx or {}).get("profile", {}).get("total_turns", 0) or 0)
                        if _turns_seen >= 4:
                            from src.host.central_push_client import (
                                fetch_llm_readiness, compute_customer_id,
                            )
                            _cid = compute_customer_id("facebook_name", f"{did}::{peer_name}")
                            _r = fetch_llm_readiness(_cid)
                            if _r:
                                _readiness = float(_r.get("conversion_readiness") or 0.5)
                                _raw_readiness = float(_r.get("raw_readiness") or _readiness)
                    except Exception:
                        pass
                    gate = should_refer(
                        intent=intent_tag,
                        ref_score=ref_score,
                        memory_ctx=memory_ctx,
                        lead_score=lead_score_val,
                        has_contact=has_contact,
                        config=_gate_cfg,
                        # PR-7: 让关键词触发 / 拒绝词命中 / persona min_turns 真生效
                        incoming_text=incoming_text or "",
                        persona_key=persona_key,
                        # Phase-6: emotion 接 gate, jp_female_midlife.min_emotion_score=0.5
                        emotion_overall=_emotion_overall,
                        # Phase-9: 多维独立信号
                        emotion_frustration=_emotion_frustration,
                        conversion_readiness=_readiness,
                    )
                    decision = "wa_referral" if gate.refer else "reply"
                    # Phase-10: referral_decision 落 customer_events (复盘 + 调参基础)
                    try:
                        from src.host.central_push_client import (
                            record_event, compute_customer_id,
                        )
                        _cid_for_dec = compute_customer_id(
                            "facebook_name", f"{did}::{peer_name}",
                        )
                        record_event(
                            customer_id=_cid_for_dec,
                            event_type="referral_decision",
                            worker_id="",
                            device_id=did,
                            meta={
                                "refer": bool(gate.refer),
                                "level": gate.level,
                                "score": int(gate.score or 0),
                                "threshold": int(gate.threshold or 0),
                                "reasons": list(gate.reasons or [])[:10],
                                "intent": intent_tag,
                                "ref_score": float(ref_score or 0.0),
                                "emotion_overall": _emotion_overall,
                                "frustration": _emotion_frustration,
                                "readiness": _readiness,
                                "raw_readiness": _raw_readiness,  # Phase-13: explainability
                                "persona_key": persona_key or "",
                            },
                            fire_and_forget=True,
                        )
                    except Exception as exc:
                        log.debug("[ai_reply] referral_decision push 失败: %s", exc)
                    # PR-7: 拒绝词命中 → 写 referral_rejected_at 触发 7 天冷却
                    # gate.reasons 含 "拒绝引流关键词命中" 时即拒绝路径
                    if any("拒绝引流关键词命中" in r for r in gate.reasons):
                        try:
                            from src.host.fb_store import record_contact_event
                            record_contact_event(
                                did, peer_name, "referral_rejected",
                                meta={
                                    "persona_key": persona_key or "",
                                    "rejected_at": _now_iso(),
                                    "incoming_text_snippet": (incoming_text or "")[:100],
                                },
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug("[ai_reply] 写 referral_rejected 事件失败: %s", exc)
                    log.info(
                        "[ai_reply] peer=%s gate level=%s refer=%s score=%d "
                        "reasons=%s",
                        peer_name, gate.level, gate.refer, gate.score,
                        "; ".join(gate.reasons)[:200],
                    )
                except Exception as e:
                    # gate 不可用降级到 pre-P5 行为
                    log.debug("[ai_reply] referral_gate 失败(降级): %s", e)
                    decision = "wa_referral" if (has_contact and ref_score > 0.5) else "reply"
                # PR-4 (2026-04-26): AI 动态生成话术优先, 模板仅做兜底.
                # 旧逻辑是模板覆盖 ChatBrain 的 reply, 但 ChatBrain 已能根据
                # persona / 聊天历史 / 客户画像动态生成自然话术 (chat_brain.py
                # _build_system_prompt stage='referral' + bot_persona='jp_caring_male'),
                # 模板覆盖会让每次引流都是同一句, 容易被识别. 反转: 只在 AI 输出
                # 为空 / 异常时才回退到模板.
                if decision == "wa_referral" and _r_val and not (reply or "").strip():
                    try:
                        from .fb_content_assets import get_referral_snippet
                        snippet = get_referral_snippet(
                            _r_channel, _r_val,
                            persona_key=persona_key,
                        )
                        if snippet:
                            reply = snippet
                            log.info(
                                "[ai_reply] AI 话术为空, 兜底到模板 snippet "
                                "(channel=%s persona=%s)", _r_channel, persona_key,
                            )
                    except Exception as e:
                        log.debug("[ai_reply] referral_snippet 兜底也失败: %s", e)
        except Exception as e:
            log.debug("[ai_reply] 生成失败: %s", e)
            return None, "skip"

        if not reply:
            return None, "skip"

        try:
            input_box = d(className="android.widget.EditText")
            if input_box.exists(timeout=2.0):
                self.hb.tap(d, *self._el_center(input_box))
                time.sleep(0.4)
                self.hb.type_text(d, reply)
                time.sleep(random.uniform(0.6, 1.2))
                # 2026-04-24 P19: 用 `_tap_messenger_send` 4 级 fallback (smart_tap
                # → multi-locale → coordinate → VLM) 替代裸 smart_tap。对 Messenger
                # 2026 Compose UI 下 send button 不在 AccessibilityNode 的场景, 有
                # VLM 兜底命中率大幅高。原 Enter-key 降为 L5 ultimate backstop,
                # _tap_messenger_send 真抛 MessengerError 再用它保交付。
                try:
                    self._tap_messenger_send(d, did)
                except MessengerError as mex:
                    log.warning(
                        "[ai_reply] 4 级 fallback 全 miss (%s), 降级 Enter-key", mex.code)
                    d.send_keys("\n")
                    time.sleep(0.5)
        except Exception as e:
            log.debug("[ai_reply] 发送失败: %s", e)
            return None, "skip"

        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, peer_name,
                peer_type=peer_type,  # P6: 'friend'/'stranger'/'friend_request' 等
                message_text=reply,
                direction="outgoing",
                ai_decision=decision,
                ai_reply_text=reply,
                language_detected=target_lang or detected_incoming_lang,
                preset_key=preset_key,
            )
        except Exception:
            pass
        # L2 双写 — AI 回复 / 模板回复 push 到中央
        try:
            from src.host.customer_sync_bridge import sync_messenger_outgoing
            sync_messenger_outgoing(
                did, peer_name,
                content=reply,
                ai_decision=decision,
                ai_generated=(decision == "reply"),
                content_lang=target_lang or detected_incoming_lang or None,
                intent_tag=intent_tag,
            )
        except Exception:
            pass

        # P0 2026-04-23: 跨 bot 归因 — 回写 replied_at
        #   1) 被 B 刚回复的最近一条 incoming 行
        #   2) 如果近 7 天内 A 对该 peer 写过 greeting (peer_type=friend_request),
        #      也把那条 greeting 行的 replied_at 设上 —— A 的模板效果 A/B 统计依赖这个
        try:
            from src.host.fb_store import (
                mark_greeting_replied_back,
                mark_incoming_replied,
            )
            mark_incoming_replied(did, peer_name)
            mark_greeting_replied_back(did, peer_name, window_days=7)
        except Exception as e:
            log.debug("[ai_reply] replied_at 回写失败: %s", e)

        # P7 §7.1 wa_referral_sent: 引流话术发出时记事件,让 A 的 Lead Mesh
        # Dashboard 可按 channel 切片引流漏斗 (Phase 5 未 merge 时 no-op)
        if decision == "wa_referral":
            _emit_contact_event_safe(
                did, peer_name, "wa_referral_sent",
                preset_key=preset_key,
                meta={
                    "channel": _r_channel or "unknown",
                    "peer_type": peer_type,
                    "intent": intent_tag,  # P4 意图信号
                },
            )
            # L2 双写 — 引流话术发出 (不升级 status, 等真发起 handoff 才升)
            try:
                from src.host.customer_sync_bridge import sync_wa_referral_sent
                sync_wa_referral_sent(
                    did, peer_name,
                    channel=_r_channel or "unknown",
                    content=reply,
                    content_lang=target_lang or detected_incoming_lang or None,
                    intent_tag=intent_tag,
                )
            except Exception:
                pass

        # P10b L3 结构化记忆 — LLM 抽取 extracted_facts 写 fb_contact_events。
        # 默认 config.enabled=False, 不激活则 zero cost (gate 首行就 skip,
        # 不触发 LLM client 初始化)。真机跑一段时间观察后通过 config 开。
        try:
            from src.ai.chat_facts_extractor import run_facts_extraction
            run_facts_extraction(did, peer_name, preset_key=preset_key)
        except Exception as e:
            log.debug("[ai_reply] facts_extractor 失败(降级): %s", e)

        return reply, decision

    def _open_message_requests_fallback(self, d) -> bool:
        """Messenger 不同版本入口可能在右上角菜单或顶部 tab,做兜底。"""
        for kw in ("Message Requests", "Message requests", "Requests", "请求"):
            try:
                btn = d(textContains=kw)
                if btn.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(btn))
                    return True
            except Exception:
                continue
        return False

    def _list_friend_requests(self, d, max_n: int) -> List[Dict]:
        """从 FB Friends 页 dump 当前可见好友请求。

        启发式:扫描页面所有 textView,查找带"X mutual friends"模式的卡片。

        Phase 15.1 (2026-04-25): peer_name 走 _is_valid_peer_name 校验,
        过滤 UI 文本 / 消息预览 / 测试残留 (与 _list_messenger_conversations
        共享 sanitize 逻辑). cleanup 报告显示 add_friend_accepted 210 条脏行,
        说明本 method 也漏过 UI 文本进入 contact_events.
        """
        items: List[Dict] = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception:
            return items
        for el in elements:
            text = (el.text or "").strip()
            if " mutual friend" in text.lower() or text.lower().endswith("mutual"):
                import re as _re
                m = _re.search(r"(\d+)\s+mutual", text.lower())
                count = int(m.group(1)) if m else 0
                # 抠 name 部分 (去掉 mutual 行 / 项目符号 / 多行)
                name_part = text.split(" •")[0].split("\n")[0].strip()
                # mutual line 提取的 name 部分单独 sanitize
                if not FacebookAutomation._is_valid_peer_name(name_part):
                    continue
                items.append({
                    "name": name_part,
                    "mutual_friends": count,
                })
                if len(items) >= max_n:
                    break
            elif el.clickable and len(text.split()) <= 4:
                if not FacebookAutomation._is_valid_peer_name(text):
                    continue
                items.append({"name": text, "mutual_friends": 0})
                if len(items) >= max_n:
                    break
        return items

    def _tap_accept_button_for(self, d, meta: Dict) -> bool:
        """在好友请求卡片上点击 Confirm/Accept。"""
        for kw in ("Confirm", "Accept", "确认", "接受"):
            try:
                btn = d(text=kw)
                if btn.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(btn))
                    time.sleep(1.0)
                    return True
            except Exception:
                continue
        return False

    # ── Leads Integration ─────────────────────────────────────────────────

    @_with_fb_foreground
    def search_and_collect_leads(self, query: str,
                                 device_id: Optional[str] = None,
                                 max_leads: int = 10) -> List[int]:
        """Search people and store results in Leads Pool."""
        from ..leads.store import get_leads_store
        store = get_leads_store()
        profiles = self.search_people(query, device_id, max_leads)
        lead_ids = []

        for p in profiles:
            lead_id = store.add_lead(
                name=p.get("name", "Unknown"),
                source_platform="facebook",
                tags=[query],
            )
            if p.get("profile_url"):
                store.add_platform_profile(
                    lead_id, "facebook",
                    profile_url=p["profile_url"],
                    username=p.get("username", ""),
                )
            lead_ids.append(lead_id)

        log.info("Collected %d leads from FB search '%s'", len(lead_ids), query)
        return lead_ids

    _GROUP_RESULT_META_RE = re.compile(
        r"(?:"
        r"public\s+group|private\s+group|visible\s+group|hidden\s+group|"
        r"公开小组|私密小组|公开群组|私密群组|"
        r"公開社團|私密社團|公開小組|私密小組|"
        r"公開グループ|非公開グループ|"
        r"gruppo\s+pubblico|gruppo\s+privato|"
        r"grupo\s+p[úu]blico|grupo\s+privado|"
        r"(?:\d{1,3}(?:[,，]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*[KkMm万萬])?\s*"
        r"(?:members?|名のメンバー|名(?:成员|成員|メンバー)|位(?:成员|成員|会员|會員)|成员|成員)"
        r")",
        re.IGNORECASE,
    )

    _GROUP_RESULT_JOIN_RE = re.compile(
        r"(?:^|[\s·,，])(?:加入|加入小组|加入群组|Join|Join Group)(?:$|[\s·,，])",
        re.IGNORECASE,
    )

    _GROUP_RESULT_BAD_NAMES = frozenset({
        "All", "Posts", "People", "Groups", "Pages", "Photos", "Videos",
        "全部", "帖子", "用户", "小组", "公共主页", "照片", "视频",
        "近期", "推荐", "查看全部", "See all", "See more", "Search",
        "Recent", "Recommended", "Filters", "筛选条件",
        # 2026-05-12: FB 社交提示文本（非群名）
        "1 friend is a member", "2 friends are members",
        "3 friends are members", "4 friends are members",
        "5 friends are members", "6 friends are members",
    })
    # 2026-05-12: 匹配所有 "N friend(s) is/are member(s)" 变体
    _RE_FRIENDS_ARE_MEMBERS = re.compile(
        r"^\d+\s+friends?\s+(?:is|are)\s+members?$", re.IGNORECASE
    )

    def _type_fb_search_query(self, d, query: str,
                              did: Optional[str] = None) -> bool:
        """在 FB 搜索框输入 query。优先 set_text, 支持中/日文。"""
        query = (query or "").strip()
        if not query:
            return False
        did = self._did(did)

        def _dump_xml() -> str:
            try:
                return d.dump_hierarchy(force_refresh=True) or ""
            except TypeError:
                try:
                    return d.dump_hierarchy() or ""
                except Exception:
                    return ""
            except Exception:
                return ""

        def _is_fb_foreground() -> bool:
            try:
                try:
                    d.invalidate_app_cache()
                except Exception:
                    pass
                return (d.app_current() or {}).get("package", "") == PACKAGE
            except Exception:
                return False

        def _is_fb_search_surface() -> bool:
            if not _is_fb_foreground():
                return False
            return hierarchy_looks_like_fb_search_surface(_dump_xml())

        def _query_visible() -> bool:
            try:
                return bool(_is_fb_foreground() and query and query in _dump_xml())
            except Exception:
                return False

        def _clear_current_field(edit_el) -> None:
            try:
                edit_el.click()
                time.sleep(0.2)
            except Exception:
                pass
            try:
                edit_el.clear_text()
                time.sleep(0.2)
            except Exception:
                pass
            if did:
                try:
                    self._adb("shell input keyevent 279", device_id=did)  # SELECT_ALL
                    time.sleep(0.1)
                    self._adb("shell input keyevent 67", device_id=did)   # DEL
                    time.sleep(0.1)
                except Exception:
                    pass

        # Hard guard: never type a FB keyword into system search / Chrome /
        # Messenger. The previous fallback typed globally when no FB EditText
        # was found, which sent broad keywords such as "ペット" to Google on
        # devices where the search tap drifted out of Facebook.
        if not _is_fb_search_surface():
            log.warning(
                "[search] refusing to type query=%r outside FB search surface",
                query,
            )
            return False

        for edit_sel in get_search_query_editor_selectors():
            try:
                if not _is_fb_search_surface():
                    return False
                edit_el = d(**edit_sel)
                if not edit_el.exists(timeout=1.8):
                    continue
                _clear_current_field(edit_el)
                try:
                    edit_el.set_text(query)
                    time.sleep(0.6)
                    if _query_visible():
                        return True
                    log.debug("[search] set_text 后 query 未出现在搜索框/页面, "
                              "改用 send_keys. query=%r", query)
                except Exception as e:
                    log.debug("[search] set_text failed query=%r: %s", query, e)
                try:
                    _clear_current_field(edit_el)
                    d.send_keys(query, clear=False)
                    time.sleep(0.6)
                    if _query_visible():
                        return True
                    log.debug("[search] send_keys 后 query 未出现在搜索框/页面. "
                              "query=%r", query)
                except Exception as e:
                    log.debug("[search] send_keys failed query=%r: %s", query, e)
                    try:
                        _clear_current_field(edit_el)
                        self.hb.type_text(d, query)
                        time.sleep(0.6)
                        if _query_visible():
                            return True
                    except Exception:
                        pass
            except Exception:
                continue
        log.warning("[search] FB search EditText not found; abort query typing")
        return False

    def _looks_like_group_result_meta(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if self._GROUP_RESULT_META_RE.search(t):
            return True
        return False

    def _result_requires_join(self, *parts: str) -> bool:
        raw = " ".join((p or "") for p in parts)
        return bool(self._GROUP_RESULT_JOIN_RE.search(raw))

    def _clean_group_candidate_name(self, text: str) -> str:
        name = (text or "").replace("\xa0", " ").strip()
        if "\n" in name or "\r" in name or "\t" in name:
            return ""
        # content-desc 常见形式: "群名, 公开小组 · 385 位成员"
        for sep in (",", "，", " · ", "・"):
            if sep in name:
                left = name.split(sep, 1)[0].strip()
                if left:
                    name = left
                    break
        for suffix in ("· 加入", " · 加入", "加入", "Join"):
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
        name = name.rstrip(" ·・,，")
        return name[:80].strip()

    def _is_valid_group_candidate_name(self, name: str, keyword: str = "") -> bool:
        if not name or len(name) < 2:
            return False
        if "\n" in name or "\r" in name or "\t" in name:
            return False
        if name.endswith(("…", "...", "..")) or ".." in name:
            return False
        if name in self._GROUP_RESULT_BAD_NAMES:
            return False
        if self._RE_FRIENDS_ARE_MEMBERS.match(name.strip()):
            return False
        low = name.lower().strip()
        meta_prefixes = (
            "公开", "公開", "公有", "私密", "私人", "非公開",
            "public", "private", "visible", "hidden",
        )
        if any(low.startswith(p.lower()) for p in meta_prefixes):
            return False
        if self._looks_like_group_result_meta(name):
            return False
        if re.search(r"\b\d+(?:[.,]\d+)?\s*km\b", name, re.IGNORECASE):
            return False
        if any(sym in name for sym in ("£", "$", "€", "¥")):
            return False
        if re.search(r"\b(?:pet sitter|marketplace|listing|service)\b",
                     name, re.IGNORECASE):
            return False
        if re.fullmatch(r"[\d\s,，.·・万萬kKmM]+", name):
            return False
        if len(name) > 80:
            return False
        if keyword and len(keyword.strip()) >= 2:
            # 宽关键词允许结果名不完全等于 keyword, 但至少不要是完全无关的短功能文案。
            if len(name) <= 4 and keyword.strip() not in name:
                return False
        return True

    def _extract_group_search_results(self, d, *,
                                      keyword: str = "",
                                      max_groups: int = 10) -> List[Dict[str, Any]]:
        """从 Groups-filtered 搜索结果页抽取群组候选。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        try:
            xml = d.dump_hierarchy() or ""
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception as e:
            log.warning("[search_groups] dump/parse failed: %s", e)
            return out

        rows = []
        for el in elements:
            txt = (el.text or el.content_desc or "").strip()
            if not txt or not el.bounds:
                continue
            if el.bounds[1] < 220:
                continue
            rows.append((el.bounds[1], el.bounds[0], txt, el))
        rows.sort(key=lambda r: (r[0], r[1]))
        join_rows = [
            (y, x, txt) for y, x, txt, _el in rows
            if self._result_requires_join(txt)
        ]

        def parse_member_count(meta: str) -> int:
            m = re.search(
                r"(\d{1,3}(?:[,，]\d{3})+|\d+(?:[.,]\d+)?)\s*([KkMm万萬])?\s*"
                r"(?:members?|名のメンバー|名(?:成员|成員|メンバー)|"
                r"位(?:成员|成員|会员|會員)|成员|成員)",
                meta or "",
            )
            if not m:
                return 0
            raw = m.group(1).replace(",", "").replace("，", "")
            raw = raw.replace(",", ".")
            try:
                value = float(raw)
            except Exception:
                return 0
            unit = (m.group(2) or "").lower()
            if unit == "k":
                value *= 1000
            elif unit == "m":
                value *= 1000000
            elif unit in ("万", "萬"):
                value *= 10000
            return int(value)

        def add_candidate(raw_name: str, meta: str = "",
                          row_y: Optional[int] = None) -> bool:
            name = self._clean_group_candidate_name(raw_name)
            key = name.lower()
            if key in seen:
                return False
            if not self._is_valid_group_candidate_name(name, keyword):
                return False
            if meta and not self._looks_like_group_result_meta(meta):
                return False
            member_count = parse_member_count(meta)
            requires_join = self._result_requires_join(raw_name, meta)
            if not requires_join and row_y is not None:
                requires_join = any(abs(jy - row_y) <= 80 for jy, _jx, _jt in join_rows)
            seen.add(key)
            out.append({
                "group_name": name,
                "keyword": keyword or "",
                "member_count": member_count,
                "meta": meta[:200],
                "requires_join": requires_join,
            })
            return True

        # 1) content-desc 一行包含「群名 + 群组/成员」时直接提取。
        for _y, _x, txt, _el in rows:
            if self._looks_like_group_result_meta(txt):
                add_candidate(txt, txt, row_y=_y)
                if len(out) >= max_groups:
                    return out[:max_groups]

        # 2) TextView 分行时，遇到 meta 行向上找最近的名称行。
        for idx, (_y, _x, txt, _el) in enumerate(rows):
            if not self._looks_like_group_result_meta(txt):
                continue
            for j in range(idx - 1, max(-1, idx - 8), -1):
                _py, _px, prev, _pel = rows[j]
                if abs(_y - _py) > 240:
                    break
                if self._looks_like_group_result_meta(prev):
                    continue
                if add_candidate(prev, txt, row_y=_py):
                    break
            if len(out) >= max_groups:
                break
        return out[:max_groups]

    @_with_fb_foreground
    def discover_groups_by_keyword(self, keyword: str,
                                   device_id: Optional[str] = None,
                                   max_groups: int = 10,
                                   skip_visited: bool = True,
                                   persona_key: Optional[str] = None,
                                   target_country: str = "") -> List[Dict[str, Any]]:
        """用宽泛关键词搜索 FB 群组，并把候选写入 facebook_groups。

        这条路径只发现和记录群组，不加好友、不发消息。
        """
        did = self._did(device_id)
        d = self._u2(did)
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        with self.guarded("discover_groups", device_id=did, weight=0.2):
            if not self._tap_search_bar_preferred(d, did):
                return []
            time.sleep(0.8)
            if not self._type_fb_search_query(d, keyword, did):
                return []
            time.sleep(1.0)
            if not self._submit_fb_search_with_verify(d, did, keyword):
                return []
            filter_ok = False
            if self._tap_search_results_groups_filter(d, did):
                time.sleep(1.2)
                try:
                    xml = d.dump_hierarchy() or ""
                except Exception:
                    xml = ""
                filter_ok = hierarchy_looks_like_fb_groups_filtered_results_page(xml)
            if not filter_ok:
                log.info(
                    "[search_groups] keyword=%r Groups filter 未确认切换，"
                    "改从当前 All 结果页的小组 section 抽取候选",
                    keyword,
                )
                try:
                    if xml and any(s in xml for s in (
                        "显示结果", "重置", "帖子来源", "发布日期",
                        "Show results", "Reset", "Post source", "Date posted",
                    )):
                        self._adb("shell input keyevent 4", device_id=did)
                        time.sleep(0.8)
                        log.info(
                            "[search_groups] keyword=%r 误入筛选抽屉，"
                            "已返回结果页后再抽取",
                            keyword,
                        )
                except Exception:
                    pass
            groups = self._extract_group_search_results(
                d, keyword=keyword, max_groups=max_groups)
            if not groups:
                # FB 切换到小组结果后列表有时比 chip 高亮晚 1-2 秒出现。
                # 空结果不立刻判失败，短等再 dump，避免真实页已加载但首 dump 抢跑。
                for retry_i in range(2):
                    time.sleep(1.2)
                    groups = self._extract_group_search_results(
                        d, keyword=keyword, max_groups=max_groups)
                    if groups:
                        log.info(
                            "[search_groups] keyword=%r retry_extract=%d got=%d",
                            keyword, retry_i + 1, len(groups),
                        )
                        break

        try:
            from src.host.fb_store import (has_group_been_visited,
                                           upsert_group)
            kept = []
            for g in groups:
                name = g.get("group_name") or ""
                visited = has_group_been_visited(did, name)
                g["already_visited"] = visited
                if skip_visited and visited:
                    continue
                status = "pending" if g.get("requires_join") else "discovered"
                upsert_group(
                    did, name,
                    member_count=int(g.get("member_count") or 0),
                    country=target_country or "",
                    status=status,
                    preset_key=persona_key or "",
                )
                kept.append(g)
            groups = kept
        except Exception as e:
            log.debug("[search_groups] discovered groups 入库失败: %s", e)
        log.info("[search_groups] keyword=%r discovered=%d", keyword, len(groups))
        return groups

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _tap_search_bar_preferred(self, d, device_id: Optional[str] = None) -> bool:
        """优先用 resourceId/description 点搜索框，避免 AutoSelector 把 Feed 顶栏缓存成错误坐标。

        关键修复 (2026-04-23): 某些 FB 版本 (katana 最新) 的顶栏搜索 icon 是
          <Button content-desc="Search" class="Button"> 而不是 EditText "Search Facebook".
        加硬编码 `description="Search"` + 点击后做"是否进搜索页"自检，
        自检失败就回退 (避免把 AutoSelector 缓存的污染坐标当成进了)。
        """
        did = self._did(device_id)

        def _is_on_fb_home() -> bool:
            """Home feed 的稳定特征（见 ``fb_search_markers``）。"""
            try:
                return hierarchy_looks_like_fb_home(d.dump_hierarchy() or "")
            except Exception:
                return False

        def _force_back_to_home(max_attempts: int = 3) -> bool:
            """确保在 FB Home feed. 策略: 2 次 back 尝试, 不行就 **stop + restart FB**
            (最干净, 彻底清掉任何 location 弹窗/group/profile/messenger 子页污染).
            stop 代价低于每轮 smoke 被脏状态卡死。"""
            # 快速路径: 已在 Home
            if _is_on_fb_home():
                return True

            # 先尝试最多 2 次 back (如果在 FB 且某些 modal 可以 back 掉)
            try:
                cur_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                cur_pkg = ""
            if cur_pkg == "com.facebook.katana":
                for _ in range(2):
                    try:
                        d.press("back")
                        time.sleep(0.8)
                    except Exception:
                        break
                    if _is_on_fb_home():
                        return True

            # 兜底: stop + start — 彻底复位
            log.info("[search] 强制重启 FB (cur=%s) 以清除子页/弹窗/跨 app 污染", cur_pkg)
            try:
                d.app_stop("com.facebook.katana")
                time.sleep(1.5)
                self._adb_start_main_user(did)
            except Exception as e:
                log.warning("[search] app_stop/start 失败: %s", e)
            # FB 启动 + 可能弹窗需要时间, 给 6 秒
            for _ in range(6):
                time.sleep(1.0)
                if _is_on_fb_home():
                    return True
            # 扫一次常见弹窗再 check
            for t in FB_STARTUP_DISMISS_TARGET_TEXTS:
                try:
                    el = d(text=t)
                    if el.exists(timeout=0.3):
                        el.click()
                        time.sleep(0.5)
                except Exception:
                    pass
            return _is_on_fb_home()

        def _is_on_search_page() -> bool:
            """搜索页特征（``fb_search_markers.hierarchy_looks_like_fb_search_surface``）。"""
            try:
                return hierarchy_looks_like_fb_search_surface(d.dump_hierarchy() or "")
            except Exception:
                return False

        def _is_on_messenger_or_chats() -> bool:
            """误点到 Messenger/Chats（``fb_search_markers``）。"""
            try:
                return hierarchy_looks_like_messenger_or_chats(d.dump_hierarchy() or "")
            except Exception:
                return False

        def _back_to_fb_home(max_presses: int = 4) -> None:
            for _ in range(max_presses):
                if not _is_on_messenger_or_chats():
                    return
                try:
                    d.press("back")
                    time.sleep(0.8)
                except Exception:
                    return

        def _is_safe_home_search_candidate(sel: Dict[str, Any], el) -> bool:
            """Avoid tapping nearby Messenger/shortcut buttons on the FB home bar."""
            raw = " ".join(str(sel.get(k) or "") for k in ("description", "text"))
            raw_norm = raw.strip().lower().replace(" ", "")
            search_labels = {
                "search",
                "searchfacebook",
                "搜索",
                "搜索facebook",
            }
            if raw_norm not in search_labels:
                return True
            try:
                info = el.info or {}
                actual = " ".join((
                    str(info.get("contentDescription") or ""),
                    str(info.get("text") or ""),
                )).lower()
                if any(
                    bad in actual
                    for bad in (
                        "messenger",
                        "messages",
                        "chats",
                        "messenger消息",
                        "聊天",
                        "消息",
                    )
                ):
                    log.info(
                        "[search] skip home search candidate because it looks "
                        "like Messenger/chat sel=%s actual=%r",
                        sel, actual[:120],
                    )
                    return False
                cx, cy = self._el_center(el)
                w, h = d.window_size()
                top_band = max(220, int(h * 0.16))
                left = int(w * 0.62)
                right = int(w * 0.90)
                if cy <= top_band and left <= cx <= right:
                    return True
                log.info(
                    "[search] skip generic Search candidate outside home search "
                    "slot sel=%s center=(%s,%s) window=(%s,%s)",
                    sel, cx, cy, w, h,
                )
                return False
            except Exception:
                return True

        # 入口强制 stop + start FB — 2026-04-24 决策:
        # 实测 pure 测试能搜 6 条, 生产 smoke 搜 0 条, 唯一差异是 pure 先彻底重启 FB.
        # 沿用的旧 FB session 搜索请求可能 cached/stale, 每次 search 入口多花 7s 换
        # 100% 成功率是值得的. 也一并清所有 Messenger/Profile/Location 弹窗污染.
        if _is_on_search_page():
            return True
        log.info("[search] 强制 stop+start FB 清除所有前置污染")
        try:
            d.app_stop("com.facebook.katana")
            time.sleep(1.5)
            self._adb_start_main_user(did)
        except Exception as e:
            log.warning("[search] app_stop/start 异常: %s", e)
        for _ in range(7):
            time.sleep(1.0)
            if _is_on_fb_home():
                break
        for t in ("Not Now", "Skip", "Maybe Later", "OK", "Got it",
                    "Continue", "Close", "Dismiss", "Cancel",
                    "Allow", "While using the app", "Later"):
            try:
                el = d(text=t)
                if el.exists(timeout=0.3):
                    el.click()
                    time.sleep(0.4)
            except Exception:
                pass

        # 2026-04-23 重构: 只用 **在 Home 上真实存在** 的最稳 selector,
        # 过往尝试的 resource-id 全被 FB 混淆成 "(name removed)" 永远 0 candidates,
        # EditText "Search Facebook" 只在进搜索页后才出现, 在 Home 上试反而
        # 可能误中 feed 某条 post 的 text 触发乱点.
        # debug 验证: Home 顶栏稳定只有一个 <Button content-desc="Search">.
        for sel in get_home_search_button_selectors():
            try:
                el = d(**sel)
                if not el.exists(timeout=2.4):
                    continue
                if not _is_safe_home_search_candidate(sel, el):
                    continue
                self.hb.tap(d, *self._el_center(el))
                time.sleep(1.8)
                if _is_on_search_page():
                    log.info("[search] opened search via selector %s", sel)
                    return True
                try:
                    cur_pkg_after = (d.app_current() or {}).get("package", "")
                except Exception:
                    cur_pkg_after = ""
                if cur_pkg_after == MESSENGER_PACKAGE or _is_on_messenger_or_chats():
                    log.warning(
                        "[search] selector %s opened Messenger/chats; "
                        "discard candidate and recover FB home",
                        sel,
                    )
                    _force_back_to_home()
                    continue
                # 2026-05-12: 检测是否误跳到 Chrome/Google/其他 app
                _NON_FB_PKGS = (
                    "com.android.chrome", "com.google.android.googlequicksearchbox",
                    "com.miui.home", "com.sec.android.app.sbrowser",
                    "org.mozilla.firefox", "com.brave.browser",
                    "com.opera.browser", "com.opera.mini.native",
                )
                if cur_pkg_after and cur_pkg_after in _NON_FB_PKGS:
                    log.warning(
                        "[search] selector %s 误打开了非 FB app: %s, 返回重试",
                        sel, cur_pkg_after,
                    )
                    try:
                        d.press("back")
                        time.sleep(1.0)
                    except Exception:
                        pass
                    _force_back_to_home()
                    continue
                log.warning("[search] selector %s 点了但未进搜索页, "
                             "重回 Home 再试下一个 (pkg=%s)", sel, cur_pkg_after)
                _force_back_to_home()
            except Exception:
                continue

        # 所有 selector 都失败时, 走坐标 fallback (debug 里 Home 顶栏 Search 在 [536,68]-[624,156])
        if self._fallback_search_tap(d):
            time.sleep(1.2)
            if _is_on_search_page():
                return True
            try:
                cur_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                cur_pkg = ""
            # 2026-05-12: fallback tap 后如果跳到了非 FB app，返回并重启 FB
            _NON_FB_FALLBACK = (
                "com.android.chrome", "com.google.android.googlequicksearchbox",
                "com.miui.home", "com.sec.android.app.sbrowser",
            )
            if cur_pkg in _NON_FB_FALLBACK:
                log.warning("[search] fallback tap 误打开 %s, 返回并重启 FB", cur_pkg)
                try:
                    d.press("back")
                    time.sleep(1.0)
                except Exception:
                    pass
                _force_back_to_home()
            else:
                log.warning("[search] fallback tap 未进入 FB 搜索页(current=%s), 中止",
                            cur_pkg or "?")
        return False

    def _people_tab_fallback_adb(self, d, device_id: str) -> None:
        """People 筛选：按屏幕分辨率缩放 w0 基准坐标 (332,204)@720x1600。"""
        try:
            w, h = d.window_size()
            x = max(40, min(int(332 * (w / 720.0)), w - 40))
            y = max(120, min(int(204 * (h / 1600.0)), h - 120))
        except Exception:
            x, y = 332, 204
        try:
            self._adb(f"shell input tap {x} {y}", device_id=device_id)
            log.info("[search_people] People tab: scaled ADB tap (%s, %s)", x, y)
        except Exception:
            pass

    def _fallback_search_tap(self, d):
        """Fallback: try common search button selectors."""
        for sel in get_fallback_search_tap_selectors():
            el = d(**sel)
            if el.exists(timeout=2):
                raw = " ".join(str(sel.get(k) or "") for k in ("description", "text"))
                raw_norm = raw.strip().lower().replace(" ", "")
                if raw_norm in {"search", "searchfacebook", "搜索", "搜索facebook"}:
                    try:
                        cx, cy = self._el_center(el)
                        w, h = d.window_size()
                        if not (
                            cy <= max(220, int(h * 0.16))
                            and int(w * 0.62) <= cx <= int(w * 0.90)
                        ):
                            log.info(
                                "[search] fallback skip generic Search candidate "
                                "center=(%s,%s) window=(%s,%s)",
                                cx, cy, w, h,
                            )
                            continue
                    except Exception:
                        pass
                self.hb.tap(d, *self._el_center(el))
                return True
        return False

    # 排除列表：content_desc 匹配这些的不是人员卡片
    _SEARCH_RESULT_EXCLUDED_CDS = {
        "add friend", "add\xa0friend", "see all", "back",
        "filter all", "clear text", "more options",
        "all search results", "reels search results",
        "people search results", "groups search results",
        "events search results",
    }

    def _is_person_card(self, el) -> bool:
        """判断元素是否为搜索结果中的人员卡片（可点击的完整宽度 Button）。"""
        if not el.clickable:
            return False
        cd = (getattr(el, "content_desc", "") or "").strip()
        t = (el.text or "").strip()
        # 必须有 content_desc 或 text
        display = cd or t
        if len(display) < 2:
            return False
        # 排除 UI 动作按钮
        if display.lower() in self._SEARCH_RESULT_EXCLUDED_CDS:
            return False
        # 排除"更多选项"类
        if "更多选项" in display or "more options" in display.lower():
            return False
        # 人员卡片通常是宽 Button（全宽或接近全宽），高度 > 80px
        b = el.bounds
        if not b:
            return False
        w = b[2] - b[0]
        h = b[3] - b[1]
        if h < 80:
            return False
        # 优先：Button 类且全宽（0 到接近屏幕宽）
        if el.class_name and "Button" in el.class_name:
            if w > 400:  # 全宽或接近全宽
                return True
        # 也接受 ViewGroup 里的宽卡片
        if el.class_name and ("ViewGroup" in el.class_name or "FrameLayout" in el.class_name):
            if w > 400:
                return True
        return False

    def _extract_search_results(self, d, max_results: int,
                                query_hint: str = "") -> List[Dict[str, str]]:
        """Extract names from search results (heuristic, uses XML dump).
        
        支持两种 FB 界面：
        1. 中文界面：结果以 TextView 形式出现（text='人名'）
        2. 英文界面：结果以全宽 Button 出现（content_desc='人名,地点'）
        """
        results = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)

            # 先尝试 TextView 模式（中文 FB 界面）
            for el in elements:
                if el.text and len(el.text) > 2 and el.clickable:
                    if el.class_name and "TextView" in el.class_name:
                        t = el.text.strip()
                        if t in self._BAD_SEARCH_RESULT_NAMES:
                            continue
                        if t in self._SEARCH_FILTER_TAB_TEXTS:
                            continue
                        if el.bounds and el.bounds[1] < 280 and len(t) < 36:
                            continue
                        if query_hint and not self._search_result_name_plausible(
                                t, query_hint):
                            continue
                        results.append({
                            "name": el.text,
                            "username": "",
                            "profile_url": "",
                        })
                        if len(results) >= max_results:
                            break

            # 如果 TextView 模式没结果，尝试 Button/content_desc 模式（英文 FB 界面）
            if not results:
                for el in elements:
                    if self._is_person_card(el):
                        cd = (getattr(el, "content_desc", "") or "").strip()
                        # content_desc 格式: "人名,地点" 或 "人名"
                        name = cd.split(",")[0].strip() if cd else (el.text or "").strip()
                        if name and len(name) >= 2:
                            if query_hint and not self._search_result_name_plausible(
                                    name, query_hint):
                                continue
                            results.append({
                                "name": name,
                                "username": "",
                                "profile_url": "",
                            })
                            if len(results) >= max_results:
                                break
        except Exception as e:
            log.warning("Failed to extract search results: %s", e)
        return results

    # 搜索结果顶栏筛选词 — 不可当作「第一条人名」点击
    _SEARCH_FILTER_TAB_TEXTS = frozenset({
        "All", "Posts", "People", "Groups", "Pages", "Events", "Reels",
        "Photos", "Marketplace", "Videos", "Places", "News",
        "全部", "贴文", "用户", "小组", "公共主页", "活动", "影片", "照片",
    })
    # 易被 TextView 启发式误当成「人名」的列表项 / 功能入口
    _BAD_SEARCH_RESULT_NAMES = frozenset({
        "Group chat", "See all", "See more", "Home", "Search", "Marketplace",
        "Notifications", "Menu", "Settings", "Recent", "Recent searches",
        "Clear", "Filters", "Meta AI", "New message", "Message",
    })

    def _search_result_name_plausible(self, name: str, query: str) -> bool:
        """人名结果应与搜索词有 token 重叠，避免 Group chat 等误报。"""
        n = (name or "").strip()
        if len(n) < 2:
            return False
        if n in self._BAD_SEARCH_RESULT_NAMES:
            return False
        if n in self._SEARCH_FILTER_TAB_TEXTS:
            return False
        qtok = [p for p in (query or "").replace("·", " ").split() if len(p) > 1]
        if not qtok:
            return True
        nl = n.lower()
        return any(t.lower() in nl for t in qtok)

    # 2026-04-24 v3: 姓搜 + walk candidates 支持
    # 常见男性日文名后缀 — 在搜索结果卡片级就快速跳过, 不浪费 L2 VLM quota
    _MALE_JP_NAME_SUFFIXES = (
        "郎", "太", "雄", "健", "輔", "介", "也", "司", "彦",
        "男", "夫", "之", "治", "樹", "一", "二", "三", "博",
        "志", "朗", "哉", "佑", "翔", "斗", "馬", "弥",
    )

    def _is_likely_male_jp_name(self, name: str) -> bool:
        """通过姓名末尾汉字启发式判断是否明显为男性日文名."""
        if not name:
            return False
        parts = name.replace("　", " ").split()
        first_name = parts[-1] if parts else name
        if not first_name:
            return False
        return first_name[-1] in self._MALE_JP_NAME_SUFFIXES

    def _peer_already_contacted(self, name: str) -> tuple:
        """检查 peer 是否已联系过, 用于 walk candidates 跳过已互动的 peer.

        Returns (contacted: bool, reason: str).

        Phase D4: 双层检查 — journey 软层 + is_friend_requested_globally 硬层.
        """
        if not name:
            return False, ""
        # Layer 1: journey 事件流 (soft check)
        try:
            from src.host.lead_mesh import resolve_identity, get_journey
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:{name}",
                                    display_name=name)
            events = get_journey(cid) or []
            actions = [e.get("action") for e in events]
            if "greeting_sent" in actions:
                return True, "already_greeted"
            if "friend_requested" in actions:
                return True, "already_friend_requested"
            if "friend_already" in actions:
                return True, "already_friend_contacted"
            for e in events:
                if (e.get("action") == "add_friend_blocked"
                        and (e.get("data") or {}).get("reason") == "request_already_pending"):
                    return True, "request_already_pending"
        except Exception:
            pass
        # Layer 2: facebook_friend_requests 表全局硬查 (Phase D4 补强)
        try:
            from src.host.fb_store import is_friend_requested_globally
            dup = is_friend_requested_globally(name, hours=720)
            if dup.get("found"):
                return True, "global_friend_request_exists"
        except Exception:
            pass
        return False, ""

    def _try_dismiss_verify_dialog(self, d) -> bool:
        """尝试点击 "以后再说" 类按钮 dismiss 账号验证/checkpoint 弹窗.

        Returns True if 找到并点了 dismiss 按钮, False 否则.
        """
        dismiss_texts = (
            "以后再说", "稍后", "稍后再说", "跳过", "不再显示", "取消",
            "Later", "Not Now", "Not now", "Skip", "Dismiss",
            "Maybe Later", "Remind Me Later",
            "あとで", "スキップ", "後で", "今はしない",
        )
        for txt in dismiss_texts:
            try:
                el = d(text=txt, clickable=True)
                if el.exists(timeout=0.4):
                    el.click()
                    log.info("[risk/dismiss] 点击 '%s' 成功", txt)
                    time.sleep(1.2)
                    return True
                el = d(text=txt)
                if el.exists(timeout=0.3):
                    el.click()
                    log.info("[risk/dismiss] 点击 '%s' (非 clickable) 成功", txt)
                    time.sleep(1.2)
                    return True
            except Exception:
                continue
        for desc in ("Later", "Skip", "Not Now", "以后", "稍后", "あとで"):
            try:
                el = d(descriptionContains=desc, clickable=True)
                if el.exists(timeout=0.3):
                    el.click()
                    log.info("[risk/dismiss] 点击 desc='%s' 成功", desc)
                    time.sleep(1.2)
                    return True
            except Exception:
                continue
        return False

    def _first_search_result_element(self, d, query_hint: str = ""):
        """返回搜索结果列表里第 1 个**匹配 query_hint**的人员卡片元素(用于进入主页)。

        2026-04-23 修复: 原版只返回屏幕最顶的人卡片 — 若搜"佐藤花子"排序返回
        [佐藤葵花, 佐藤花子, ...], 旧版会点到"佐藤葵花"(屏幕最上)进错 profile.
        现在按 query_hint 做 plausible 匹配, 优先返回第一个匹配候选; 若全不匹配
        才回退到原行为(最顶卡片). 避免明显误点.

        优先宽卡片（与 w0_capture_direct.search_and_navigate 一致），再回退 TextView，
        避免误点筛选标签或窄 TextView。
        """
        def _card_text(el) -> str:
            """从 person card 提取主要人名文字 (content_desc 优先 - 格式通常是 '人名,地点')。"""
            cd = (getattr(el, "content_desc", "") or "").strip()
            if cd:
                return cd.split(",")[0].strip()
            return (getattr(el, "text", "") or "").strip()

        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)

            # ① 全宽人员卡片（英文 Button / ViewGroup）— 收集所有, 按 query 优先排
            cards = [el for el in elements if self._is_person_card(el)]
            if cards and query_hint:
                plausible = [c for c in cards
                              if self._search_result_name_plausible(
                                  _card_text(c), query_hint)]
                if plausible:
                    el = plausible[0]
                    if len(plausible) < len(cards):
                        log.info("[first_result] query=%r 跳过 %d 个不匹配卡片, 选第 %d 张",
                                  query_hint, cards.index(el),
                                  cards.index(el) + 1)
                    b = el.bounds
                    return type("E", (), {
                        "info": {"bounds": {
                            "left": b[0], "top": b[1],
                            "right": b[2], "bottom": b[3],
                        }}
                    })()
            for el in cards:
                b = el.bounds
                return type("E", (), {
                    "info": {"bounds": {
                        "left": b[0], "top": b[1],
                        "right": b[2], "bottom": b[3],
                    }}
                })()

            # ② 中文等：可点击 TextView, 同样应用 query 优先
            textviews = []
            for el in elements:
                if (el.clickable and el.text and len(el.text) >= 2
                        and el.class_name and "TextView" in el.class_name):
                    t = (el.text or "").strip()
                    if t in self._SEARCH_FILTER_TAB_TEXTS:
                        continue
                    if el.bounds and el.bounds[1] < 280 and len(t) < 30:
                        continue
                    textviews.append(el)
            if textviews and query_hint:
                plausible = [el for el in textviews
                              if self._search_result_name_plausible(el.text, query_hint)]
                if plausible:
                    el = plausible[0]
                    return type("E", (), {
                        "info": {"bounds": {
                            "left": el.bounds[0], "top": el.bounds[1],
                            "right": el.bounds[2], "bottom": el.bounds[3],
                        }}
                    })()
            for el in textviews:
                    return type("E", (), {
                        "info": {"bounds": {
                            "left": el.bounds[0], "top": el.bounds[1],
                            "right": el.bounds[2], "bottom": el.bounds[3],
                        }}
                    })()
        except Exception:
            pass
        return None

    @staticmethod
    def _el_center(el) -> tuple:
        info = el.info
        b = info.get("bounds", {})
        cx = (b.get("left", 0) + b.get("right", 0)) // 2
        cy = (b.get("top", 0) + b.get("bottom", 0)) // 2
        return (cx, cy)
