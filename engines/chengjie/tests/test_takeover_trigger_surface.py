# -*- coding: utf-8 -*-
"""「查看≠接管」触发面门禁（impl85 阶段2，2026-08-29，工单#45 钧拍板口径）。

客户拍板的判定规则（值守群内 2026-08-29 凌晨逐条对齐，duty_replies.jsonl）：
- 查看类动作（打开会话 / 看信息页 / 切面板 / mark-read / presence 心跳）
  **一律不算**人工接管，永不停全自动；
- 停全自动只认：人工发出内容（发消息/图/语音/贴纸/点建议回复走发送链）、
  显式操作（一键接管 / 下拉框选档 / 搁置静音 / 工单结案勾转人工 / 主管批量降档）、
  系统守卫（防轰炸降档），以及全局待机等运营开关；
- 30 分钟自动接回判定保留；不做「下一条客户消息进来立即接回」；不补答。

本门禁把上述触发面钉成 ratchet：任何新增「会把会话切档」的调用点必须先过这里
（加进允许清单=显式决策），往查看类路由塞档位写入会立刻变红。
2026-08-29 全量审计结论：现存全部写点都属显式操作/守卫，**零查看类触发**——
与值守修正后的口径一致（「只点开查看不会停 AI，系统本来就是这个行为」）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SRC = REPO / "src"

# ── 允许清单：会写会话档位（set_automation_mode）的文件 → 触发性质 ──────────────
_MODE_WRITER_ALLOWLIST = {
    "src/inbox/store.py":                                  "定义处（set/bulk 本体）",
    "src/inbox/takeover_rearm.py":                         "人工出站接管 + 静默超时自动接回",
    "src/inbox/takeover.py":                               "一键接管/交还（显式按钮）",
    "src/inbox/snooze_hold.py":                            "搁置静音（显式操作）",
    "src/inbox/automation_mode.py":                        "首入站 bootstrap（写全局默认档）",
    "src/inbox/peer_bot_guard.py":                         "防轰炸守卫降档（guard:*/sweep）",
    "src/companion/standby_mode.py":                       "全局待机模式（运营开关）",
    "src/web/routes/unified_inbox_aggregate.py":           "坐席下拉框显式选档（source=human）",
    "src/web/routes/unified_inbox_account_routes.py":      "垃圾会话预置 manual（新会话预建）",
    "src/web/routes/unified_inbox_stored_read_routes.py":  "主管一键批量降档（显式应急）+ D-M9 切手动「30 分钟后接回」来源改写（显式选项）",
    "src/inbox/account_bulk_mode.py":                      "M-2 B（D-M1 ②）账号菜单「此账号全部会话 → 手动/半自动/全自动」——坐席显式操作 + 二次确认，全自动唯一开启口",
    "src/inbox/account_mode_onboarding.py":                "J-2 C（#63 #167）新账号接管方式确认后对齐系统落档行（align_account_conversations，只动 bootstrap/standby/account_mode 来源）——运营显式决策",
    "src/web/routes/cases_routes.py":                      "工单结案勾「转人工」（显式勾选）",
    "src/web/routes/web_chat_routes.py":                   "网站访客会话默认档（新会话预建）",
    "src/inbox/stop_contact.py":                           "O-1 A（#252 #253 · D-O1）客户要求停联 / 自伤硬停守卫：冻结 → manual（guard:stop_contact_from:<原档>），人工解冻还原——守卫类，与 peer_bot_guard 同族",
    "src/assistant/actions.py":                            "助手显式「对齐档位 / 撤销对齐」：坐席确认后批量写档，不是打开会话",
    "src/inbox/commitment_guard.py":                       "承诺守卫二次坚持：auto_ai → review（source=guard:commitment），守卫降档",
    "src/inbox/dormant_review.py":                         "沉睡会话三按钮里「转手动」：坐席显式点选，不是查看",
}

# ── 允许清单：调 record_agent_takeover（接管即静音）的文件 ─────────────────────
_TAKEOVER_CALLER_ALLOWLIST = {
    "src/inbox/takeover_rearm.py":                    "定义处",
    "src/inbox/automation_mode_stats.py":             "docstring 提及（观测计数）",
    "src/web/routes/unified_inbox_send_routes.py":    "人工发送：文本/媒体/语音",
    "src/web/routes/sticker_routes.py":               "人工发送：贴纸",
    "src/inbox/channel_adapters.py":                  "网站客服坐席人工出站",
    "src/integrations/wechat_kf_webhook.py":          "坐席显式点「转企微人工」（POST /kf/transfer）成功后打接管标：企微坐席在答、本端 AI 必须停手（实施97 线 A）",
}

# ── 查看类路由文件：绝不允许出现任何档位写入 / 接管调用 ─────────────────────────
#   unified_inbox_read_routes.py   = 会话列表 / thread 读取 / mark-read（打开会话）
#   unified_inbox_workspace_presence_routes.py = 坐席在场心跳（打开页面就有）
_VIEW_PATH_FILES = (
    "src/web/routes/unified_inbox_read_routes.py",
    "src/web/routes/unified_inbox_workspace_presence_routes.py",
)

_FORBIDDEN_IN_VIEW = ("set_automation_mode(", "record_agent_takeover",
                      "bulk_set_automation_mode(")


def _files_containing(needle: str) -> set:
    out = set()
    for p in SRC.rglob("*.py"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if needle in text:
            out.add(p.relative_to(REPO).as_posix())
    return out


def test_mode_writer_files_are_allowlisted():
    """新增「会把会话切档」的文件必须先进允许清单（显式决策，防查看类触发悄悄溜进来）。"""
    found = _files_containing("set_automation_mode(")
    extra = found - set(_MODE_WRITER_ALLOWLIST)
    assert not extra, (
        f"发现未登记的档位写入文件：{sorted(extra)}——"
        "「查看≠接管」是客户拍板口径（工单#45），新增写点先核对触发性质"
        "（人工发内容/显式操作/守卫才允许），再登记进 _MODE_WRITER_ALLOWLIST。")


def test_takeover_caller_files_are_allowlisted():
    found = _files_containing("record_agent_takeover")
    extra = found - set(_TAKEOVER_CALLER_ALLOWLIST)
    assert not extra, (
        f"发现未登记的接管调用文件：{sorted(extra)}——"
        "接管即静音只允许挂在「人工发出内容」的出站动作上（#45 拍板），先核对再登记。")


def test_view_paths_never_write_mode():
    """查看类路由（thread 读取 / mark-read / presence 心跳）绝不写档位。

    往这些文件里加 set_automation_mode / record_agent_takeover ＝ 重新引入
    「点开查看就停全自动」——本条变红即为设计红线被踩。
    """
    for rel in _VIEW_PATH_FILES:
        p = REPO / rel
        assert p.exists(), f"查看类路由文件不存在（改名了要同步本门禁）：{rel}"
        text = p.read_text(encoding="utf-8", errors="replace")
        for needle in _FORBIDDEN_IN_VIEW:
            assert needle not in text, (
                f"{rel} 出现 {needle} —— 查看类路径禁止触发接管/改档（工单#45 拍板）")


def test_allowlists_not_stale():
    """允许清单里的文件必须真实存在且仍含对应调用——防清单腐化成摆设。"""
    for rel in _MODE_WRITER_ALLOWLIST:
        p = REPO / rel
        assert p.exists(), f"_MODE_WRITER_ALLOWLIST 陈旧：{rel} 不存在"
        assert "set_automation_mode(" in p.read_text(encoding="utf-8", errors="replace"), (
            f"_MODE_WRITER_ALLOWLIST 陈旧：{rel} 已无档位写入，请移除")
    for rel in _TAKEOVER_CALLER_ALLOWLIST:
        p = REPO / rel
        assert p.exists(), f"_TAKEOVER_CALLER_ALLOWLIST 陈旧：{rel} 不存在"
        assert "record_agent_takeover" in p.read_text(encoding="utf-8", errors="replace"), (
            f"_TAKEOVER_CALLER_ALLOWLIST 陈旧：{rel} 已无接管调用，请移除")


# ── 行为钉：接管写入语义（manual + takeover source）与查看零写入的最小对照 ──────

class _FakeStore:
    def __init__(self):
        self.writes = []

    def get_automation_mode_meta(self, cid):
        return {"mode": "auto_ai", "source": "bootstrap"}

    def set_automation_mode(self, cid, mode, *, source=""):
        self.writes.append((cid, mode, source))


def test_record_agent_takeover_semantics_pinned():
    """人工出站 → manual + takeover_from:<原档>（30min 接回据此恢复原档）。"""
    from src.inbox.takeover_rearm import record_agent_takeover
    store = _FakeStore()
    src = record_agent_takeover(store, "line:acct:U1")
    assert store.writes == [("line:acct:U1", "manual", "takeover_from:auto_ai")]
    assert src == "takeover_from:auto_ai"


def test_mark_read_store_call_is_watermark_only():
    """mark-read 路由对 store 的期望面＝已读水位（mark_conversation_read），
    不含档位写入——配合 test_view_paths_never_write_mode 双保险。"""
    p = REPO / "src/web/routes/unified_inbox_read_routes.py"
    text = p.read_text(encoding="utf-8", errors="replace")
    assert "mark_conversation_read" in text
    m = re.search(r"async def api_unified_inbox_mark_read.*?(?=\n    @app\.|\Z)",
                  text, re.S)
    assert m, "mark-read 路由不见了（改名要同步本门禁）"
    body = m.group(0)
    assert "set_automation_mode" not in body
    assert "record_agent_takeover" not in body
