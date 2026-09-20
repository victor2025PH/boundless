# -*- coding: utf-8 -*-
"""小智「动作注册表」纯函数核心（实施58 P1，2026-08-23）。

设计铁律（docs/实施58 §3.4；改前先读）：
1. **白名单即全部**：智能体/前端只能提名本表登记的动作；执行器只写表内
   声明的 config 路径（与 reply_pacing_settings.FIELDS / capability_toggle
   同哲学——结构性防线，不靠 prompt 自觉）。**不造新写端点**：全部经
   ``cm.set_overlay_flag``（ruamel 保注释）落 overlay。
2. **权限分级**：L0 只读查询 / L1 教学导航 / L2 可逆设置（必须确认 +
   可撤销）。给客户发消息等 L3 刻意不进本批（走既有人审投递链，P2 再议）。
3. **先看后做**：L2 动作两段式——``plan_action`` 出 diff（现值→新值），
   ``issue_confirm`` 发一次性确认 token（TTL 10min），带 token 才
   ``apply_plan``；应用登记 undo 快照（进程内 24h + 审计 JSONL 落盘）。
4. **审计**：每次写/撤销追加 ``assistant_actions_audit.jsonl``（config 目录，
   风格对齐 companion_capability_audit.jsonl）。

本模块保持零 web 依赖（纯函数 + 进程级小状态），路由薄包装在
``src/web/routes/assistant_action_routes.py``。门禁
``tests/test_assistant_actions.py``。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.assistant import ui_anchors
from src.assistant import runner_pairing

logger = logging.getLogger(__name__)

CONFIRM_TTL_SEC = 600.0
UNDO_TTL_SEC = 24 * 3600.0
_MAX_PENDING = 50

_time = time.time  # 测试可 monkeypatch

_LOCK = threading.Lock()
_CONFIRMS: Dict[str, Dict[str, Any]] = {}
_UNDOS: Dict[str, Dict[str, Any]] = {}

# goto 白名单兜底（nav_schema 提取失败时的保守核心集）
CORE_NAV_PATHS = (
    "/workspace",
    "/reply-settings",
    "/personas",
    "/knowledge",
    "/ops-overview",
    "/dashboard",
    "/membership",
    "/settings",
    "/rpa-overview",   # L2 动作 home（能力看板）——兜底集必须覆盖全部 home
)

# ── 值→人话标签表（确认卡 diff 的 old_h/new_h；老板令 2026-08-23：界面
#    绝不出现 L2/config 路径这类黑话，值也要说人话）─────────────────────
_VL_MODE = {
    "manual": ("纯手动", "manual"),
    "review": ("AI拟稿·人审后发", "AI drafts, human approves"),
    "multi_choice": ("AI出多稿·人来挑", "AI multi-draft, human picks"),
    "auto_ai": ("全自动", "fully automatic"),
}
_VL_TRIGGER = {
    "never": ("从不发语音", "never"),
    "always": ("每条都语音", "every reply"),
    "when_peer_voice": ("对方发语音才回语音", "when peer sends voice"),
    "smart": ("智能判断", "smart"),
}
_VL_LENGTH = {
    "": ("跟随人设", "follow persona"),
    "concise": ("简短", "concise"),
    "moderate": ("适中", "moderate"),
    "detailed": ("详细", "detailed"),
}
_VL_EMOJI = {
    "": ("跟随人设", "follow persona"),
    "none": ("不用表情", "none"),
    "minimal": ("偶尔一个", "minimal"),
    "moderate": ("适度", "moderate"),
    "rich": ("丰富", "rich"),
}

# 平台键域与 reply_pacing_settings.PLATFORMS 同源（本模块保持零依赖，
# 一致性由 tests/test_assistant_actions.py 钉住）。
ACTION_PLATFORMS = ("telegram", "whatsapp", "line", "messenger", "zalo",
                    "instagram",
                    # QQ 协议登录（个人号，Milky；2026-09-07）：有状态 worker，进能力矩阵
                    "qq")

# ── 注册表 ────────────────────────────────────────────────────────────────
# level: L0 查询 / L1 导航 / L2 可逆设置。
# fields: L2 允许写的 config 点分路径（default=快照缺键时的真实缺省，
#         与 reply_pacing_settings.FIELDS 对齐）；hot 语义同该表——
#         True=消费方活读 config 写完即生效；False/缺省=重启窗后全量生效
#         （前端如实展示，绝不谎报）。path 含 {platform} 的字段须带
#         platforms 白名单，参数里必须给 platform。
# home: 该设置在后台的所在页面（2026-08-30，「小智原来会带我去页面，现在
#       不带了」实录）——导航步此前全凭 LLM 规划时的自由发挥，8-23/27 的
#       计划带 goto、之后的不带。validate_plan 据本字段在改设置步骤前
#       **确定性**插入「带你去」导航步（用户已在该页/白名单外则不插），
#       不再靠 prompt 自觉。FIELDS 家族的键都渲染在 /reply-settings，
#       companion 能力开关在 /rpa-overview 能力看板。
ACTIONS: Dict[str, Dict[str, Any]] = {
    "diagnose_autoreply": {
        "level": "L0",
        "kind": "query",
        "label_zh": "诊断：为什么不自动回复",
        "label_en": "Diagnose: why is auto-reply off",
        "desc_zh": "汇总影响全自动回复的因素清单（因素注册表，只读）",
        "desc_en": "Read-only factor checklist affecting auto-reply",
    },
    "query_ai_status": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：AI 现在正常吗",
        "label_en": "Query: is the AI healthy",
        "desc_zh": "主链/备用/本地兜底当前谁在岗（只读）",
        "desc_en": "Read-only: primary / pool / local fallback duty status",
    },
    "query_reply_budget": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：今日回复额度",
        "label_en": "Query: today's reply budget",
        "desc_zh": "回复额度保险丝今天的用量与触顶情况（只读）",
        "desc_en": "Read-only: today's per-chat reply budget usage",
    },
    "query_platform_health": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：平台账号在线吗",
        "label_en": "Query: platform sessions health",
        "desc_zh": "各平台会话（messenger/whatsapp 等）当前健康状况（只读）",
        "desc_en": "Read-only: current platform session health",
    },
    "query_version": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：软件当前版本",
        "label_en": "Query: current app version",
        "desc_zh": "当前应用版本号（只读；报障/核对更新用）",
        "desc_en": "Read-only: current application version (for support / "
                   "update checks)",
    },
    "goto_page": {
        "level": "L1",
        "kind": "nav",
        "label_zh": "带我去指定页面",
        "label_en": "Take me to a page",
        "desc_zh": "跳转到导航白名单内的页面（params.path）",
        "desc_en": "Navigate to a whitelisted page (params.path)",
    },
    # ── DOM 动作总线（实施88 P0，2026-08-30）：在页面上替用户做「揭示类」
    #    手势。目标只能是 ui_anchors.UI_ANCHORS 注册表内的控件（LLM 提名
    #    anchor id，选择器由服务端换出）；手势仅 click（点开抽屉/表单）/
    #    fill（填搜索框）/ show（聚光带看）三类，发送/提交/删除永不进表。
    "ui_act": {
        "level": "L1",
        "kind": "ui",
        "label_zh": "在页面上帮你点开/填入/带看（白名单控件）",
        "label_en": "Operate a whitelisted on-page control (open / fill / "
                    "show)",
        "desc_zh": "params.anchor=控件白名单 id；fill 类控件需带 "
                   "params.text（≤80 字搜索词）；只做揭示类手势，不发送不删除",
        "desc_en": "params.anchor = whitelisted control id; fill controls "
                   "need params.text (search term ≤80 chars); reveal-only "
                   "gestures, never send / delete",
    },
    # ── Windows 操控 runner 只读侦察（实施91 P1-1，2026-08-30）：看一台
    #    受控机当前在干什么。machine=配对白名单机器 id（runner_pairing 换出
    #    base_url），tool=只读三工具，read_tree/screenshot 需 window 标题。
    #    L0 只读、无确认卡；受 assistant.pc_runner.enabled 总闸（默认关）+
    #    机器白名单 + runner 侧 token 三重闸。动作面（launch/click）P1-2 才开。
    "pc_inspect": {
        "level": "L0",
        "kind": "runner",
        "label_zh": "看一台电脑现在在干什么（只读）",
        "label_en": "Inspect what a PC is doing now (read-only)",
        "desc_zh": "只读侦察受控机：params.machine=白名单机器 id，"
                   "params.tool=list_windows|read_tree|screenshot，"
                   "read_tree/screenshot 需 params.window（窗口标题）；"
                   "只看不改",
        "desc_en": "Read-only inspect a controlled PC: params.machine = "
                   "whitelisted id, params.tool = list_windows|read_tree|"
                   "screenshot, read_tree/screenshot need params.window; "
                   "look but never touch",
        "tools": ("list_windows", "read_tree", "screenshot"),
    },
    # ── Windows 操控 runner 动作面（实施91 P1-2）：L2 确认卡（复用同一确认/
    #    审计骨架，不开第二条写通道）。app_id 只认 _PC_APP_ALLOWED 结构性白名单，
    #    runner 侧 APP_WHITELIST + PC_RUNNER_ACTIONS + kill-switch 再校验一次
    #    （纵深）。click 留 P1-3（需服务端解析点击目标，绝不吃 LLM 坐标）。
    "pc_launch": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "在电脑上打开一个应用",
        "label_en": "Open an app on the PC",
        "desc_zh": "在受控机启动白名单应用：params.machine=白名单机器 id，"
                   "params.app_id=notepad|calc|explorer|mspaint（只认这几个）；"
                   "需人工确认",
        "desc_en": "Launch a whitelisted app on a controlled PC: "
                   "params.machine = whitelisted id, params.app_id = "
                   "notepad|calc|explorer|mspaint (only these); needs confirm",
        "tools": ("launch_app",),
    },
    "pc_focus": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "把电脑上某个窗口切到前台",
        "label_en": "Bring a window to front on the PC",
        "desc_zh": "把受控机某窗口置前台：params.machine=白名单机器 id，"
                   "params.window=窗口标题（子串匹配）；需人工确认",
        "desc_en": "Bring a window to front on a controlled PC: "
                   "params.machine = whitelisted id, params.window = title "
                   "substring; needs confirm",
        "tools": ("focus_window",),
    },
    # ── P2-A 全能力档（实施91，2026-08-31）：确认档下可执行任意提议动作，
    #    每个仍走 L2 确认卡 + runner 三重闸 + 输入消毒。信任档免确认属 P2-C。
    "pc_run_app": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "在电脑上运行一个程序（任意路径）",
        "label_en": "Run a program on the PC (any path)",
        "desc_zh": "在受控机启动任意可执行文件：params.machine=白名单机器 id，"
                   "params.path=程序绝对路径（可选 params.args 参数列表）；需人工确认",
        "desc_en": "Launch any executable on a controlled PC: params.machine = "
                   "whitelisted id, params.path = absolute exe path (optional "
                   "params.args list); needs confirm",
        "tools": ("launch_path",),
    },
    "pc_click": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "点击电脑上的一个控件",
        "label_en": "Click a control on the PC",
        "desc_zh": "点击受控机某控件（须先 pc_inspect read_tree 拿到控件）："
                   "params.machine, params.window=窗口标题, "
                   "params.target=控件的 auto_id 或名字；仅点唯一可调用控件、无坐标；"
                   "需人工确认",
        "desc_en": "Click a control on a controlled PC (run pc_inspect read_tree "
                   "first): params.machine, params.window=title, "
                   "params.target=control auto_id or name; invoke-only, no "
                   "coords; needs confirm",
        "tools": ("click_control",),
    },
    "pc_type": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "在电脑上输入文字",
        "label_en": "Type text on the PC",
        "desc_zh": "把文字填进受控机当前窗口的输入框：params.machine, "
                   "params.window=窗口标题, params.text=要输入的文字；需人工确认",
        "desc_en": "Type text into the focused field on a controlled PC: "
                   "params.machine, params.window=title, params.text=text; "
                   "needs confirm",
        "tools": ("type_text",),
    },
    "pc_keys": {
        "level": "L2",
        "kind": "runner",
        "label_zh": "在电脑上按快捷键",
        "label_en": "Press a keyboard shortcut on the PC",
        "desc_zh": "向受控机窗口发快捷键：params.machine, params.window=窗口标题, "
                   "params.keys=快捷键（如 {Ctrl}s、{Alt}{F4}、{Enter}）；需人工确认",
        "desc_en": "Send a keyboard shortcut to a controlled PC window: "
                   "params.machine, params.window=title, params.keys=keys "
                   "(e.g. {Ctrl}s, {Alt}{F4}, {Enter}); needs confirm",
        "tools": ("send_keys",),
    },
    "pc_click_target": {
        "level": "L2",
        "kind": "runner",
        # 视觉点击点的是 VLM 定位出的**裸坐标**，绕过 click_control 的控件名禁区闸
        # （_CLICK_DENY_RE），且坐标本就是模型猜的——像 run_command 一样**永远弹确认
        # 卡**（即便信任档 P2-C 也不免确认），确保每次视觉点击都有人复核落点。
        "always_confirm": True,
        "label_zh": "在电脑上按描述点击（视觉定位）",
        "label_en": "Click on the PC by description (visual)",
        "desc_zh": "当控件没有可用结构（画布/自绘、click 点不到）时按自然语言描述"
                   "点击：params.machine, params.window=窗口标题, params.target=要点"
                   "什么的描述（如「蓝色的提交按钮」）；服务端截图经视觉模型定位换"
                   "坐标（坐标不由你给）、runner 越界兜底；⚠坐标是模型定位、永远需"
                   "人工确认（信任档也不免）",
        "desc_en": "Click by natural-language description when a control has no "
                   "usable structure (canvas/custom): params.machine, "
                   "params.window=title, params.target=what to click (e.g. 'the "
                   "blue Submit button'); the server screenshots + a vision model "
                   "resolves coordinates (you do NOT supply coords); coords are "
                   "model-located so it ALWAYS needs confirm (even in trusted mode)",
        "tools": ("click_target",),
    },
    "pc_run_command": {
        "level": "L2",
        "kind": "runner",
        # 命令执行永远弹确认卡——即使将来信任档（P2-C）对别的动作免确认，
        # run_command 也永不免（一条注入就能删库）。P2-C 信任档须读此标记。
        "always_confirm": True,
        "label_zh": "在电脑上运行命令（PowerShell）",
        "label_en": "Run a command on the PC (PowerShell)",
        "desc_zh": "在受控机执行 PowerShell 命令并回显输出：params.machine, "
                   "params.command；⚠高危、需人工确认（信任档也不免确认）",
        "desc_en": "Run a PowerShell command on a controlled PC and return "
                   "output: params.machine, params.command; DANGEROUS, always "
                   "needs confirm",
        "tools": ("run_command",),
    },
    "set_reply_delay": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整回复速度（拟人延迟区间）",
        "label_en": "Adjust reply speed (humanized delay range)",
        "desc_zh": "min_sec/max_sec 秒级区间，0-600；写 overlay + 活体 worker 热更",
        "desc_en": "min_sec/max_sec in 0-600s; overlay write + live worker hot-apply",
        "hot": "worker",
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.deliver_delay.min_sec",
             "param": "min_sec", "type": "number", "lo": 0, "hi": 600,
             "default": 0,
             "label_zh": "回复延迟下限（秒）", "label_en": "Min delay (s)"},
            {"path": "inbox.l2_autosend.deliver_delay.max_sec",
             "param": "max_sec", "type": "number", "lo": 0, "hi": 600,
             "default": 0,
             "label_zh": "回复延迟上限（秒）", "label_en": "Max delay (s)"},
        ],
    },
    "toggle_voice_reply": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关自动语音回复",
        "label_en": "Toggle automatic voice replies",
        "desc_zh": "AI 自动回复时是否用语音条回（热生效）",
        "desc_en": "Whether auto replies go out as voice notes (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.voice.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "自动语音回复", "label_en": "Auto voice replies"},
        ],
    },
    # ── 2026-08-23 动作池扩容（老板令「能做多少做多少」）。全部复用既有
    #    overlay 写面与 reply_pacing_settings.FIELDS 登记过的路径，零新端点。
    "set_automation_mode": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "切换回复档位（全自动/人审/手动）",
        "label_en": "Switch reply mode (auto / review / manual)",
        "desc_zh": "全局默认档位 + 同步存量会话的固化档位（关全自动=所有全自动"
                   "会话一起停；开全自动只抬系统写入的行，不覆盖人的设置；"
                   "活读热生效）",
        "desc_en": "Global default plus stock alignment of pinned "
                   "conversations (turning auto off stops every auto chat; "
                   "turning it on only lifts system-written rows; hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.auto_draft.automation_mode", "param": "mode",
             "type": "enum",
             "choices": ("manual", "review", "multi_choice", "auto_ai"),
             "default": "auto_ai", "vl": _VL_MODE,
             "label_zh": "回复档位", "label_en": "Reply mode"},
        ],
    },
    "set_platform_automation_cap": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "限某平台的自动化档位",
        "label_en": "Cap automation on one platform",
        "desc_zh": "对单个平台限档（如 messenger 只许 AI 拟稿人审）；"
                   "设回「全自动」即不限（活读热生效）",
        "desc_en": "Cap one platform (e.g. messenger review-only); set back "
                   "to fully-automatic to lift the cap (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.auto_draft.platform_modes.{platform}",
             "param": "mode", "type": "enum",
             "choices": ("manual", "review", "multi_choice", "auto_ai"),
             "default": None, "vl": _VL_MODE,
             "platforms": ACTION_PLATFORMS,
             "label_zh": "该平台档位上限", "label_en": "Platform cap"},
        ],
    },
    "set_reply_length": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整回复长度（简短/适中/详细）",
        "label_en": "Set reply length (concise / moderate / detailed)",
        "desc_zh": "全局默认回复长度；人设里显式设置的优先（热生效）",
        "desc_en": "Global default reply length; persona overrides win (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "ai.reply_defaults.length", "param": "length",
             "type": "enum",
             "choices": ("", "concise", "moderate", "detailed"),
             "default": "", "vl": _VL_LENGTH,
             "label_zh": "回复长度", "label_en": "Reply length"},
        ],
    },
    "set_emoji_level": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整表情用量",
        "label_en": "Set emoji usage",
        "desc_zh": "回复里 emoji 的密度：不用/偶尔/适度/丰富（热生效）",
        "desc_en": "Emoji density in replies: none / minimal / moderate / "
                   "rich (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "ai.reply_defaults.emoji_level", "param": "level",
             "type": "enum",
             "choices": ("", "none", "minimal", "moderate", "rich"),
             "default": "", "vl": _VL_EMOJI,
             "label_zh": "表情用量", "label_en": "Emoji usage"},
        ],
    },
    "set_max_sentences": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "限制每条回复的句数",
        "label_en": "Cap sentences per reply",
        "desc_zh": "每条回复最多几句话，0=不限制（热生效）",
        "desc_en": "Max sentences per reply, 0 = unlimited (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "ai.reply_defaults.max_sentences", "param": "n",
             "type": "number", "lo": 0, "hi": 10, "default": 0,
             "label_zh": "每条最多句数", "label_en": "Max sentences"},
        ],
    },
    "set_tone_hint": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "设置全局语气提示",
        "label_en": "Set global tone hint",
        "desc_zh": "一句话语气要求（如「多关心对方，少用感叹号」），"
                   "留空=清除（热生效）",
        "desc_en": "One-line tone request (e.g. 'be caring, fewer "
                   "exclamations'); empty = clear (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "ai.reply_defaults.tone_hint", "param": "hint",
             "type": "text", "maxlen": 120, "default": "",
             "label_zh": "语气提示", "label_en": "Tone hint"},
        ],
    },
    "set_voice_trigger": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "设定什么时候用语音回",
        "label_en": "When to reply with voice",
        "desc_zh": "从不/每条都语音/对方发语音才回语音/智能判断（热生效）",
        "desc_en": "never / always / when peer sends voice / smart (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.voice.trigger", "param": "trigger",
             "type": "enum",
             "choices": ("never", "always", "when_peer_voice", "smart"),
             "default": "when_peer_voice", "vl": _VL_TRIGGER,
             "label_zh": "语音回复时机", "label_en": "Voice trigger"},
        ],
    },
    "toggle_work_schedule": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关上下班班表",
        "label_en": "Toggle work-hours schedule",
        "desc_zh": "开=按班表时间回复，关=全天可回（热生效）",
        "desc_en": "On = reply only in work hours; off = around the clock "
                   "(hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.work_schedule.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "上下班班表", "label_en": "Work schedule"},
        ],
    },
    "toggle_reply_budget_guard": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关每日回复额度保险丝",
        "label_en": "Toggle daily reply budget guard",
        "desc_zh": "防对面也是机器人互刷：单个聊天每天回复条数保险丝（热生效）",
        "desc_en": "Anti bot-loop fuse: caps replies per chat per day (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.peer_bot_guard.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "回复额度保险丝", "label_en": "Reply budget guard"},
        ],
    },
    "toggle_outbound_translate": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关出站自动翻译",
        "label_en": "Toggle outbound auto-translation",
        "desc_zh": "发出前自动译成客户语言；开关重启窗后全量生效",
        "desc_en": "Translate outgoing messages into the customer's "
                   "language; takes full effect after the restart window",
        "hot": False,
        "home": "/rpa-overview",
        "fields": [
            {"path": "inbox.l2_autosend.translate.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "出站自动翻译", "label_en": "Outbound translation"},
        ],
    },
    "toggle_proactive_greeting": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关主动打招呼",
        "label_en": "Toggle proactive greetings",
        "desc_zh": "客户沉默几小时后 AI 主动问候；开关重启窗后全量生效",
        "desc_en": "AI reaches out after hours of silence; takes full "
                   "effect after the restart window",
        "hot": False,
        "home": "/rpa-overview",
        "fields": [
            {"path": "companion.proactive_topic.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "主动打招呼", "label_en": "Proactive greetings"},
        ],
    },
    "toggle_daily_ritual": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关早晚安问候",
        "label_en": "Toggle morning/night greetings",
        "desc_zh": "按客户活跃时间点发早安晚安；开关重启窗后全量生效",
        "desc_en": "Personalized good-morning / good-night pings; takes "
                   "full effect after the restart window",
        "hot": False,
        "home": "/rpa-overview",
        "fields": [
            {"path": "companion.proactive_topic.daily_ritual.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "早晚安问候", "label_en": "Daily ritual"},
        ],
    },
    "toggle_selfie": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关聊天发生活照",
        "label_en": "Toggle photo sharing in chat",
        "desc_zh": "人设相册/自拍链总开关（客户要图时才发，护栏照旧）；"
                   "开关重启窗后全量生效",
        "desc_en": "Master switch for persona photo sharing (guardrails "
                   "unchanged); takes full effect after the restart window",
        "hot": False,
        "home": "/rpa-overview",
        "fields": [
            {"path": "companion.selfie.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "聊天发生活照", "label_en": "Photo sharing"},
        ],
    },
    # ── 2026-08-30 动作池扩容第二批（实施88 P0）。仍全部复用
    #    reply_pacing_settings.FIELDS 登记过的路径（门禁钉死），零新写端点。
    "set_typing_indicator": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关「正在输入…」状态",
        "label_en": "Toggle the typing indicator",
        "desc_zh": "AI 回复前是否显示打字状态（拟人化；活体 worker 热更）",
        "desc_en": "Show a typing indicator before AI replies (humanize; "
                   "hot-applied to the live worker)",
        "hot": "worker",
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.typing_indicator", "param": "enabled",
             "type": "bool", "default": True,
             "label_zh": "打字状态", "label_en": "Typing indicator"},
        ],
    },
    "set_mark_read": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关回复前先标记已读",
        "label_en": "Toggle mark-as-read before replying",
        "desc_zh": "AI 回复前是否先把客户消息标为已读（拟人化；活体 worker "
                   "热更）",
        "desc_en": "Mark the customer's message as read before replying "
                   "(humanize; hot-applied to the live worker)",
        "hot": "worker",
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.mark_read_before_reply",
             "param": "enabled", "type": "bool", "default": True,
             "label_zh": "回复前已读", "label_en": "Read before reply"},
        ],
    },
    "toggle_adaptive_delay": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关智能回复延迟",
        "label_en": "Toggle adaptive reply delay",
        "desc_zh": "按生成耗时自动抵扣拟人延迟（回复更快但仍像真人；活体 "
                   "worker 热更）",
        "desc_en": "Deduct generation time from the humanized delay "
                   "(faster yet human-like; hot-applied to the live worker)",
        "hot": "worker",
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.deliver_delay.adaptive",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "智能延迟", "label_en": "Adaptive delay"},
        ],
    },
    "toggle_bubbles": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关消息分条发送",
        "label_en": "Toggle multi-bubble replies",
        "desc_zh": "长回复拆成多条小气泡、条间带拟人节奏（热生效）",
        "desc_en": "Split long replies into several bubbles with humanized "
                   "gaps (hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.reply_style.bubbles.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "消息分条", "label_en": "Multi-bubble"},
        ],
    },
    "set_daily_reply_budget": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整每日回复额度（0=不限）",
        "label_en": "Set the daily reply budget (0 = unlimited)",
        "desc_zh": "单个聊天每天最多自动回复几条（防机器人互刷保险丝；"
                   "0=不限额，热生效）",
        "desc_en": "Max auto replies per chat per day (anti bot-loop fuse; "
                   "0 = unlimited, hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.peer_bot_guard.daily_reply_budget", "param": "n",
             "type": "number", "lo": 0, "hi": 1_000_000, "default": 500,
             "label_zh": "每日回复额度（0=不限）",
             "label_en": "Daily reply budget (0 = unlimited)"},
        ],
    },
    "set_work_hours": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "设置上下班时间",
        "label_en": "Set work hours",
        "desc_zh": "默认班表的上班/下班时间（HH:MM，可跨夜；两个都留空=清除；"
                   "班表总开关是另一个动作；热生效）",
        "desc_en": "Default schedule start / end (HH:MM, overnight OK; both "
                   "empty = clear; the schedule master switch is a separate "
                   "action; hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.work_schedule.default.start", "param": "start",
             "type": "hhmm", "default": "",
             "label_zh": "上班时间", "label_en": "Work start"},
            {"path": "inbox.work_schedule.default.end", "param": "end",
             "type": "hhmm", "default": "",
             "label_zh": "下班时间", "label_en": "Work end"},
        ],
    },
    "toggle_holding": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关回复攒批缓发",
        "label_en": "Toggle reply holding (batch & send)",
        "desc_zh": "客户连发多条时先攒一会儿再合并回复（更像真人；热生效）",
        "desc_en": "Briefly hold rapid-fire messages and reply once "
                   "(more human; hot)",
        "hot": True,
        "home": "/reply-settings",
        "fields": [
            {"path": "inbox.l2_autosend.holding.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "攒批缓发", "label_en": "Reply holding"},
        ],
    },
}


def allowed_levels_for_role(role: str) -> tuple:
    """坐席只许查询+导航；管理侧角色才许 L2 设置写。"""
    if str(role or "").strip().lower() == "agent":
        return ("L0", "L1")
    return ("L0", "L1", "L2")


def catalog(role: str, lang: str = "zh") -> List[Dict[str, Any]]:
    en = str(lang or "").lower().startswith("en")
    levels = allowed_levels_for_role(role)
    out: List[Dict[str, Any]] = []
    for aid, spec in ACTIONS.items():
        if spec["level"] not in levels:
            continue
        out.append({
            "id": aid,
            "level": spec["level"],
            "kind": spec["kind"],
            "label": spec["label_en" if en else "label_zh"],
            "desc": spec["desc_en" if en else "desc_zh"],
            "hot": spec.get("hot"),
        })
    return out


def _dig(config: Any, path: str, default: Any = None) -> Any:
    cur = config
    for part in str(path).split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _coerce(spec: Dict[str, Any], raw: Any) -> tuple:
    """(ok, value|reason)。宁拒不猜：类型/边界不对一律 bad_params。"""
    t = spec.get("type")
    if t == "bool":
        if isinstance(raw, bool):
            return True, raw
        return False, f"{spec.get('param')} must be bool"
    if t == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False, f"{spec.get('param')} must be number"
        v = float(raw)
        lo, hi = spec.get("lo"), spec.get("hi")
        if lo is not None and v < lo:
            return False, f"{spec.get('param')} < {lo}"
        if hi is not None and v > hi:
            return False, f"{spec.get('param')} > {hi}"
        return True, (int(v) if float(v).is_integer() else v)
    if t == "enum":
        if not isinstance(raw, str):
            return False, f"{spec.get('param')} must be string"
        if raw not in (spec.get("choices") or ()):
            return False, f"{spec.get('param')} not in choices"
        return True, raw
    if t == "text":
        if not isinstance(raw, str):
            return False, f"{spec.get('param')} must be string"
        s = raw.strip()
        maxlen = int(spec.get("maxlen") or 0)
        if maxlen and len(s) > maxlen:
            return False, f"{spec.get('param')} > {maxlen} chars"
        return True, s
    if t == "hhmm":
        # 班表时刻：空串=清除；否则 HH:MM（24h），H:MM 归一化为零填充
        if not isinstance(raw, str):
            return False, f"{spec.get('param')} must be string"
        s = raw.strip()
        if not s:
            return True, ""
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", s)
        if not m:
            return False, f"{spec.get('param')} must be HH:MM"
        return True, f"{int(m.group(1)):02d}:{m.group(2)}"
    return False, "unsupported type"


def _humanize(spec: Dict[str, Any], val: Any, zh: bool) -> str:
    """值→人话（确认卡展示；老板令：不给用户看 true/false/auto_ai 黑话）。"""
    if isinstance(val, bool):
        return ("开" if val else "关") if zh else ("on" if val else "off")
    vl = spec.get("vl")
    if isinstance(vl, dict) and isinstance(val, str) and val in vl:
        return vl[val][0 if zh else 1]
    if val is None:
        return "未设置" if zh else "unset"
    if val == "":
        return "（空）" if zh else "(empty)"
    return str(val)


# Windows 操控动作面 app 白名单（实施91 P1-2）：LLM 只能提名这几个 app_id，
# runner 侧 APP_WHITELIST 会再校验一次（纵深防御）。labels 供确认卡人话展示。
_PC_APP_ALLOWED = ("notepad", "calc", "explorer", "mspaint")
_PC_APP_LABELS = {
    "notepad": ("记事本", "Notepad"),
    "calc": ("计算器", "Calculator"),
    "explorer": ("文件资源管理器", "File Explorer"),
    "mspaint": ("画图", "Paint"),
}


def plan_action(
    action_id: str,
    params: Optional[Mapping[str, Any]],
    config: Any,
    *,
    nav_paths: Optional[set] = None,
    lang: str = "zh",
) -> Dict[str, Any]:
    """校验 + 出计划（纯函数，不写任何东西）。

    返回：{ok, action, level, kind, ...}；L2 附
    diff=[{path, old, new, label, old_h, new_h}]（label/old_h/new_h 是
    确认卡人话素材，按 lang 出）+ clean_params（复验用干净参数，含
    platform 这类路径模板参数）；L1 goto 附 goto=path。
    失败：{ok:False, error, detail}。
    """
    spec = ACTIONS.get(str(action_id or ""))
    if not spec:
        return {"ok": False, "error": "unknown_action", "detail": str(action_id)}
    params = params or {}
    zh = not str(lang or "").lower().startswith("en")
    base = {"ok": True, "action": action_id, "level": spec["level"],
            "kind": spec["kind"], "hot": spec.get("hot")}

    if spec["kind"] == "query":
        return base

    if spec["kind"] == "nav":
        path = str(params.get("path") or "").strip()
        allowed = nav_paths if nav_paths else set(CORE_NAV_PATHS)
        if not path or path not in allowed:
            return {"ok": False, "error": "path_not_allowed", "detail": path}
        base["goto"] = path
        return base

    if spec["kind"] == "ui":
        # DOM 动作总线（实施88 P0）：LLM 只提名锚点 id，选择器由注册表换出
        # ——前端永远不执行 LLM 生出的选择器。fill 类必须带非空 text。
        aid2 = str(params.get("anchor") or "").strip()
        anchor = ui_anchors.get_anchor(aid2)
        if not anchor:
            return {"ok": False, "error": "bad_params",
                    "detail": f"anchor '{aid2}' not registered"}
        text = ""
        if anchor["gesture"] == "fill":
            raw_t = params.get("text")
            if not isinstance(raw_t, str) or not raw_t.strip():
                return {"ok": False, "error": "bad_params",
                        "detail": "text required for fill anchor"}
            text = raw_t.strip()
            if len(text) > ui_anchors.FILL_TEXT_MAXLEN:
                return {"ok": False, "error": "bad_params",
                        "detail": f"text > {ui_anchors.FILL_TEXT_MAXLEN} chars"}
        base["ui"] = {"anchor": aid2, "sel": anchor["sel"],
                      "gesture": anchor["gesture"], "page": anchor["page"],
                      "text": text}
        base["ui_label"] = anchor["label_zh" if zh else "label_en"]
        base["clean_params"] = ({"anchor": aid2, "text": text}
                                if text else {"anchor": aid2})
        return base

    if spec["kind"] == "runner":
        # runner 侦察/动作（实施91）：LLM 只提名白名单机器 id + 工具，base_url 由
        # runner_pairing 换出、执行在路由层调 runner_client。绝不接受裸 URL。
        # L0=只读侦察（tool 由 LLM 从只读白名单提名）；L2=动作面（tool 由 spec
        # 钉死、不吃 params.tool，且出确认卡 diff）。
        machine = str(params.get("machine") or "").strip()
        if not runner_pairing.get_machine(machine):
            return {"ok": False, "error": "machine_not_available",
                    "detail": machine}
        allowed_tools = spec.get("tools") or ()
        if spec["level"] == "L0":
            tool = str(params.get("tool") or "").strip()
            if tool not in allowed_tools:
                return {"ok": False, "error": "bad_params",
                        "detail": f"tool '{tool}' not a read-only inspect tool"}
        else:
            # 动作面：单工具动作，tool 由 spec 钉死（结构性，绝不吃 params.tool）
            tool = allowed_tools[0] if allowed_tools else ""
        r_args: Dict[str, Any] = {}
        _WIN_TOOLS = ("read_tree", "screenshot", "focus_window",
                      "click_control", "type_text", "send_keys", "click_target")
        if tool in _WIN_TOOLS:
            win = params.get("window")
            if not isinstance(win, str) or not win.strip():
                return {"ok": False, "error": "bad_params",
                        "detail": "window required"}
            r_args["window"] = win.strip()[:120]
        if tool == "launch_app":
            app_id = str(params.get("app_id") or "").strip()
            if app_id not in _PC_APP_ALLOWED:
                return {"ok": False, "error": "bad_params",
                        "detail": f"app '{app_id}' not in whitelist"}
            r_args["app_id"] = app_id
        elif tool == "launch_path":
            path = str(params.get("path") or "").strip()
            if not path:
                return {"ok": False, "error": "bad_params",
                        "detail": "path required"}
            r_args["path"] = path[:400]
            xa = params.get("args")
            if isinstance(xa, list) and xa:
                r_args["args"] = [str(a)[:200] for a in xa][:20]
        elif tool in ("click_control", "click_target"):
            target = str(params.get("target") or "").strip()
            if not target:
                return {"ok": False, "error": "bad_params",
                        "detail": "target required"}
            r_args["target"] = target[:200]
        elif tool == "type_text":
            text = params.get("text")
            if not isinstance(text, str) or text == "":
                return {"ok": False, "error": "bad_params",
                        "detail": "text required"}
            r_args["text"] = text[:2000]
        elif tool == "send_keys":
            keys = str(params.get("keys") or "").strip()
            if not keys:
                return {"ok": False, "error": "bad_params",
                        "detail": "keys required"}
            r_args["keys"] = keys[:200]
        elif tool == "run_command":
            cmd = str(params.get("command") or "").strip()
            if not cmd:
                return {"ok": False, "error": "bad_params",
                        "detail": "command required"}
            r_args["command"] = cmd[:2000]
        base["runner"] = {"machine": machine, "tool": tool, "args": r_args}
        clean: Dict[str, Any] = {"machine": machine, "tool": tool}
        clean.update(r_args)
        base["clean_params"] = clean
        # L2 动作面：出确认卡素材（复用现有确认卡 label/old→new 渲染，前端零改）
        if spec["level"] != "L0":
            zh_label = str(spec.get("label_zh" if zh else "label_en") or "")
            if tool == "launch_app":
                app = r_args.get("app_id", "")
                nm = _PC_APP_LABELS.get(app, (app, app))[0 if zh else 1]
                new_h = f"{nm} @{machine}"
            elif tool == "launch_path":
                new_h = f"{r_args.get('path', '')} @{machine}"
            elif tool == "click_control":
                new_h = (f"{r_args.get('target', '')} @{machine}"
                         f"（{r_args.get('window', '')}）")
            elif tool == "click_target":
                new_h = (f"『{r_args.get('target', '')}』(视觉定位) @{machine}"
                         f"（{r_args.get('window', '')}）")
            elif tool == "type_text":
                new_h = (f"「{str(r_args.get('text', ''))[:40]}」→ "
                         f"{r_args.get('window', '')} @{machine}")
            elif tool == "send_keys":
                new_h = (f"{r_args.get('keys', '')} → "
                         f"{r_args.get('window', '')} @{machine}")
            elif tool == "run_command":
                new_h = f"⚠ {str(r_args.get('command', ''))[:80]} @{machine}"
            else:  # focus_window
                new_h = f"{r_args.get('window', '')} @{machine}"
            base["diff"] = [{"path": "pc." + tool, "old": "", "new": new_h,
                             "label": zh_label, "old_h": "—", "new_h": new_h}]
        return base

    # settings（L2）：逐字段校验 + 现值快照 + 人话标签
    diff: List[Dict[str, Any]] = []
    clean: Dict[str, Any] = {}
    for f in spec.get("fields", []):
        if f["param"] not in params:
            return {"ok": False, "error": "bad_params",
                    "detail": f"missing {f['param']}"}
        ok, v = _coerce(f, params[f["param"]])
        if not ok:
            return {"ok": False, "error": "bad_params", "detail": str(v)}
        path = str(f["path"])
        if "{platform}" in path:
            plat = str(params.get("platform") or "").strip().lower()
            allowed_p = f.get("platforms") or ()
            if plat not in allowed_p:
                return {"ok": False, "error": "bad_params",
                        "detail": f"platform not in {list(allowed_p)}"}
            path = path.replace("{platform}", plat)
            clean["platform"] = plat
        old = _dig(config, path, f.get("default"))
        clean[f["param"]] = v
        diff.append({
            "path": path, "old": old, "new": v,
            "label": str(f.get("label_zh" if zh else "label_en") or ""),
            "old_h": _humanize(f, old, zh),
            "new_h": _humanize(f, v, zh),
        })
    # 动作级横向校验
    if action_id == "set_reply_delay":
        vals = {d["path"].rsplit(".", 1)[-1]: d["new"] for d in diff}
        if vals.get("min_sec", 0) > vals.get("max_sec", 0):
            return {"ok": False, "error": "bad_params",
                    "detail": "min_sec > max_sec"}
    if action_id == "set_work_hours":
        # 半配置态（只有一头有值）＝work_hours_gate 视为未配置不闸，用户却
        # 以为设好了——宁拒不猜；跨夜班（start > end）合法不拦。
        vals = {d["path"].rsplit(".", 1)[-1]: d["new"] for d in diff}
        if bool(vals.get("start")) != bool(vals.get("end")):
            return {"ok": False, "error": "bad_params",
                    "detail": "start/end must both be set or both empty"}
    base["diff"] = diff
    base["clean_params"] = clean
    return base


# ── runner 动作免确认判定（实施91 P2-C 信任档）───────────────────────────────
def runner_auto_execute(*, kind: str, level: str, always_confirm: bool,
                        trust_enabled: bool, is_trusted: bool) -> bool:
    """runner 动作是否**免确认卡直接执行**（纯判定，路由据此决定走直执行 or 确认卡）：

    - L0 只读侦察：永远直执行（无副作用）；
    - L2 动作面：**仅当**信任档全局开启 + 该机受信 + **非 always_confirm** 才免确认
      （run_command 等 always_confirm 动作永不免——即使受信也弹确认卡）。
    非 runner 一律 False（不归本判定）。"""
    if str(kind) != "runner":
        return False
    if str(level) == "L0":
        return True
    return bool(trust_enabled and is_trusted and not always_confirm)


# ── 确认 token（单次核销 + TTL） ─────────────────────────────────────────
def _gc(store: Dict[str, Dict[str, Any]], ttl: float) -> None:
    now = _time()
    dead = [k for k, v in store.items() if now - v.get("ts", 0) > ttl]
    for k in dead:
        store.pop(k, None)


def issue_confirm(plan: Mapping[str, Any], uid: str) -> str:
    token = "xza-" + secrets.token_urlsafe(18)
    with _LOCK:
        _gc(_CONFIRMS, CONFIRM_TTL_SEC)
        if len(_CONFIRMS) >= _MAX_PENDING:
            oldest = min(_CONFIRMS, key=lambda k: _CONFIRMS[k]["ts"])
            _CONFIRMS.pop(oldest, None)
        _CONFIRMS[token] = {"plan": dict(plan), "uid": str(uid),
                            "ts": _time()}
    return token


def pop_confirm(token: str, uid: str) -> Optional[Dict[str, Any]]:
    """取走即删（单次核销）；过期/uid 不符=None。"""
    with _LOCK:
        _gc(_CONFIRMS, CONFIRM_TTL_SEC)
        ent = _CONFIRMS.pop(str(token or ""), None)
    if not ent:
        return None
    if ent.get("uid") != str(uid):
        return None
    return ent.get("plan")


# ── 应用 + 撤销 + 审计 ───────────────────────────────────────────────────
def _audit_path(cm) -> Optional[Path]:
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "assistant_actions_audit.jsonl") if base else None


def _audit(cm, rec: Dict[str, Any]) -> None:
    try:
        p = _audit_path(cm)
        if p is None:
            return
        rec = dict(rec)
        rec["ts"] = round(_time(), 3)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写小智动作审计失败（忽略）", exc_info=True)


def apply_plan(plan: Mapping[str, Any], cm, actor: str) -> Dict[str, Any]:
    """把 L2 计划写 overlay；成功登记 undo 快照并审计。

    返回 {ok, applied:[path], failed:[{path,msg}], undo_id}；ok=至少一条成功。
    审计行携带 undo_id（P1 任务历史：历史列表据此标注「仍可撤销」）——
    故先写盘收集成功项、登记 undo 快照拿到 id，再统一落审计。
    """
    diff = list(plan.get("diff") or [])
    applied: List[str] = []
    failed: List[Dict[str, str]] = []
    olds: List[Dict[str, Any]] = []
    ok_rows: List[Dict[str, Any]] = []
    for d in diff:
        try:
            ok, msg = cm.set_overlay_flag(d["path"], d["new"])
        except Exception as ex:  # cm 异常按失败记，绝不半途抛
            ok, msg = False, str(ex)
        if ok:
            applied.append(d["path"])
            olds.append({"path": d["path"], "old": d["old"]})
            ok_rows.append(d)
        else:
            failed.append({"path": d["path"], "msg": str(msg or "")})
    undo_id = ""
    if olds:
        undo_id = "xzu-" + secrets.token_urlsafe(12)
        with _LOCK:
            _gc(_UNDOS, UNDO_TTL_SEC)
            if len(_UNDOS) >= _MAX_PENDING:
                oldest = min(_UNDOS, key=lambda k: _UNDOS[k]["ts"])
                _UNDOS.pop(oldest, None)
            _UNDOS[undo_id] = {"action": plan.get("action"), "olds": olds,
                               "actor": actor, "ts": _time()}
    for d in ok_rows:
        _audit(cm, {"op": "apply", "actor": actor,
                    "action": plan.get("action"), "path": d["path"],
                    "old": d["old"], "new": d["new"], "undo_id": undo_id})
    return {"ok": bool(applied), "applied": applied, "failed": failed,
            "undo_id": undo_id}


# ── set_automation_mode 的存量会话对齐（2026-08-30 事故沉淀）──────────────
# 根因：全局默认档只对「无显式档位行」的会话生效；bootstrap/一键全自动/坐席
# 设置会把档位固化成 conversation_settings 显式行——只写 overlay 的档位切换
# 对这些会话是空话。zhiliao 实录：全局默认早已 review，31 个显式 auto_ai 行
# 照常 A 线直发，用户让小智「关闭全自动回复」→ 审计 old=review new=review
# 纯空转，小智却报「全部完成/已热更生效」。
# 方向不对称（与 standby「一键全自动」的 align 同哲学、范围刻意不同）：
# - 降档（目标 != auto_ai）＝安全方向 + 管理员显式指令 → **所有**显式 auto_ai
#   行一律降到目标档（含 human 来源与群——确认卡先展示同步规模，用户知情
#   后才应用，且可撤销）；manual/review/multi_choice 行不动（本就不自动发）。
# - 升档（目标 == auto_ai）＝风险方向 → 只抬**系统来源**（bootstrap/standby/
#   assistant*）的行，人的显式决定（human/takeover/guard/sweep…）绝不覆盖；
#   群一律跳过（群的全自动只能走 confirm_group 闸，判不出也按群算＝保守）。
ALIGN_ACTION = "set_automation_mode"
_ALIGN_UPGRADE_SOURCES = frozenset(
    {"bootstrap", "standby", "assistant", "assistant_undo"})


def _conv_is_group(store: Any, cid: str) -> bool:
    """群判定（会话行元数据 + id 启发式双判据）；判不出按群算（保守）。"""
    try:
        from src.inbox.automation_mode import conversation_is_group
        from src.inbox.ingest import is_group_conversation
        return bool(
            conversation_is_group(store, cid)
            or is_group_conversation({
                "conversation_id": cid,
                "platform": cid.split(":", 1)[0]}))
    except Exception:
        return True


def _alignment_rows(store: Any, target_mode: str) -> List[Dict[str, Any]]:
    """选出需要对齐的显式档位行（纯读）：[{cid, mode, source, is_group}]。"""
    if store is None or not hasattr(store, "list_automation_mode_rows"):
        return []
    if target_mode not in ("manual", "review", "multi_choice", "auto_ai"):
        return []
    try:
        rows = store.list_automation_mode_rows() or []
    except Exception:
        logger.debug("读取显式档位行失败（对齐跳过）", exc_info=True)
        return []
    upgrade = target_mode == "auto_ai"
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cid = str(r.get("conversation_id") or "")
            cur = str(r.get("automation_mode") or "")
            src = str(r.get("source") or "")
            if not cid or cur == target_mode:
                continue
            if upgrade:
                if src not in _ALIGN_UPGRADE_SOURCES:
                    continue
                if _conv_is_group(store, cid):
                    continue
                is_group = False
            else:
                if cur != "auto_ai":
                    continue
                is_group = _conv_is_group(store, cid)
            out.append({"cid": cid, "mode": cur, "source": src,
                        "is_group": is_group})
        except Exception:
            continue
    return out


def preview_mode_alignment(store: Any, target_mode: str) -> Dict[str, int]:
    """确认卡预览（纯读，零写入）：{total, groups}。"""
    rows = _alignment_rows(store, target_mode)
    return {"total": len(rows),
            "groups": sum(1 for r in rows if r["is_group"])}


def align_conversation_modes(
    store: Any, target_mode: str, *,
    cm: Any = None, actor: str = "", source: str = "assistant",
) -> Dict[str, Any]:
    """执行存量对齐（逐行写 store，单行异常跳过不中断）。

    返回 {changed, groups, olds:[{cid, mode, source}]}；olds 供
    ``attach_undo_conversations`` 挂进 undo 快照。降档时顺带作废该会话
    pending 的 L2 自动发草稿（与收件箱 UI 降档同语义——否则队列里的旧稿
    在 deliver 开着的部署上仍会被自动发出）。cm 给定时落一行审计
    （op=align_modes，read_history 不展示，追溯用）。
    """
    rows = _alignment_rows(store, target_mode)
    olds: List[Dict[str, Any]] = []
    changed = groups = cancelled = 0
    downgrade = target_mode != "auto_ai"
    for r in rows:
        try:
            try:
                store.set_automation_mode(r["cid"], target_mode, source=source)
            except TypeError:   # 旧 store 无 source 形参
                store.set_automation_mode(r["cid"], target_mode)
            changed += 1
            groups += 1 if r["is_group"] else 0
            olds.append({"cid": r["cid"], "mode": r["mode"],
                         "source": r["source"]})
            if downgrade and hasattr(store, "cancel_pending_l2_drafts"):
                try:
                    cancelled += int(store.cancel_pending_l2_drafts(
                        r["cid"], decided_by="mode_downgraded") or 0)
                except Exception:
                    pass
        except Exception:
            continue
    if cm is not None and changed:
        _audit(cm, {"op": "align_modes", "actor": actor,
                    "action": ALIGN_ACTION, "target": target_mode,
                    "n": changed, "groups": groups, "cancelled": cancelled})
    return {"changed": changed, "groups": groups, "cancelled": cancelled,
            "olds": olds}


def attach_undo_conversations(
    undo_id: str, olds: List[Dict[str, Any]],
) -> None:
    """把对齐明细挂进既有 undo 快照——撤销时会话档位随 overlay 键一起回。"""
    if not undo_id or not olds:
        return
    with _LOCK:
        ent = _UNDOS.get(str(undo_id))
        if ent is not None:
            ent["conv_olds"] = [dict(o) for o in olds]


# ── L0 查询的人话摘要器（P2）：路由取快照、这里出一句话——纯函数可测，
#    前端 execStep 的 query 分支只认 result.verdict，零前端改动。─────────
def summarize_ai_status(snap: Optional[Mapping[str, Any]],
                        lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    if not isinstance(snap, Mapping):
        return ("AI 状态读不到（观测口未装载）" if zh
                else "AI status unavailable (probe not loaded)")
    degraded = bool(snap.get("degraded"))
    mode = str(snap.get("mode") or "primary")
    names = {
        "primary": ("主链在岗", "primary on duty"),
        "pool": ("备用 Key 顶班", "backup key on duty"),
        "local": ("本地兜底顶班", "local fallback on duty"),
        "canned": ("兜底话术顶班", "canned replies on duty"),
    }
    label = names.get(mode, (mode, mode))[0 if zh else 1]
    if degraded:
        return (f"AI 降级中：{label}——回复仍在出，建议看运营总览定位主链问题"
                if zh else
                f"AI degraded: {label} — replies still flowing; check ops "
                "overview for the primary-chain issue")
    return (f"AI 正常：{label}" if zh else f"AI healthy: {label}")


def summarize_version(version: str, lang: str = "zh") -> str:
    """query_version 的人话结论行（取不到版本号也如实说，不编）。"""
    zh = not str(lang or "").lower().startswith("en")
    v = str(version or "").strip()
    if not v:
        return ("版本号读取不到（可能是源码部署，非安装包）" if zh
                else "Version unavailable (probably a source deployment)")
    return (f"当前软件版本：v{v}" if zh else f"Current app version: v{v}")


def summarize_reply_budget(n_rows: int, n_exhausted: int, n_relieved: int,
                           guard: Optional[Mapping[str, Any]],
                           lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    g = guard if isinstance(guard, Mapping) else {}
    enabled = bool(g.get("enabled"))
    budget = int(g.get("daily_reply_budget") or 0)
    if not enabled or budget <= 0:
        return ("回复额度保险丝未开启（今天不限量）" if zh
                else "Reply budget guard is off (no cap today)")
    if n_rows <= 0:
        return (f"额度保险丝开着（每聊每天 {budget} 条），今天还没有会话用到额度"
                if zh else
                f"Guard on ({budget}/chat/day); no chats used budget today")
    if zh:
        out = f"今天 {n_rows} 个会话在用额度（每聊上限 {budget} 条）"
        out += f"，其中 {n_exhausted} 个触顶" if n_exhausted else "，无触顶"
        if n_relieved:
            out += f"，{n_relieved} 个已豁免继续"
        return out
    out = f"{n_rows} chats used budget today (cap {budget}/chat)"
    out += (f"; {n_exhausted} hit the cap" if n_exhausted else "; none capped")
    if n_relieved:
        out += f"; {n_relieved} relieved"
    return out


def summarize_platform_sessions(dump: Optional[Mapping[str, Any]],
                                lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    d = dump if isinstance(dump, Mapping) else {}
    sessions = d.get("sessions") if isinstance(d.get("sessions"), Mapping) \
        else {}
    unhealthy = d.get("unhealthy") if isinstance(d.get("unhealthy"), list) \
        else []
    if not sessions:
        return ("没有外部平台会话在登记（telegram 协议号不走这个口）" if zh
                else "No external platform sessions registered (protocol "
                     "accounts are not tracked here)")
    if not unhealthy:
        return (f"全部平台会话健康（{len(sessions)} 条在册）" if zh
                else f"All platform sessions healthy ({len(sessions)} "
                     "registered)")
    heads = ", ".join(str(k) for k in unhealthy[:4])
    more = len(unhealthy) - 4
    tail = (f" 等{len(unhealthy)}条" if zh else f" (+{more} more)") \
        if more > 0 else ""
    return ((f"{len(unhealthy)} 条会话不健康：{heads}{tail}——"
             "可去运营总览「平台会话健康」卡重新登录") if zh else
            f"{len(unhealthy)} unhealthy: {heads}{tail} — relogin from the "
            "ops overview card")


def has_undo(undo_id: str) -> bool:
    """该撤销快照仍在有效期内（任务历史标注「可撤销」用）。"""
    if not undo_id:
        return False
    with _LOCK:
        _gc(_UNDOS, UNDO_TTL_SEC)
        return str(undo_id) in _UNDOS


_FIELD_BY_PATH: Optional[Dict[str, Dict[str, Any]]] = None
_FIELD_TPL_PREFIXES: List[tuple] = []


def _field_maps() -> tuple:
    """path→字段 spec 惰性索引（含模板路径前缀表），历史行人话化用。"""
    global _FIELD_BY_PATH, _FIELD_TPL_PREFIXES
    if _FIELD_BY_PATH is None:
        exact: Dict[str, Dict[str, Any]] = {}
        prefixes: List[tuple] = []
        for spec in ACTIONS.values():
            for f in spec.get("fields", []):
                p = str(f["path"])
                if "{platform}" in p:
                    prefixes.append((p.split("{platform}", 1)[0], f))
                else:
                    exact[p] = f
        _FIELD_BY_PATH = exact
        _FIELD_TPL_PREFIXES = prefixes
    return _FIELD_BY_PATH, _FIELD_TPL_PREFIXES


def describe_path(path: str, lang: str = "zh") -> tuple:
    """(字段人话标签, spec|None)。模板路径按前缀归位并附平台名。"""
    zh = not str(lang or "").lower().startswith("en")
    exact, prefixes = _field_maps()
    p = str(path or "")
    f = exact.get(p)
    if f is not None:
        return str(f.get("label_zh" if zh else "label_en") or p), f
    for pre, f2 in prefixes:
        if p.startswith(pre):
            plat = p[len(pre):]
            base = str(f2.get("label_zh" if zh else "label_en") or p)
            return f"{base}（{plat}）" if zh else f"{base} ({plat})", f2
    return p.rsplit(".", 2)[-1], None


def humanize_value(path: str, val: Any, lang: str = "zh") -> str:
    """按字段 spec 出人话值；未知路径回落 str。"""
    zh = not str(lang or "").lower().startswith("en")
    _label, spec = describe_path(path, lang)
    return _humanize(spec or {}, val, zh)


def read_history(cm, limit: int = 40) -> List[Dict[str, Any]]:
    """审计 JSONL 尾部 N 行（新→旧）。文件缺席/坏行一律跳过不抛。"""
    p = _audit_path(cm)
    if p is None or not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 131072))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("读小智动作审计失败", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("op") in ("apply", "undo"):
            out.append(rec)
    out.reverse()
    return out[: max(1, int(limit or 40))]


def apply_undo(
    undo_id: str, cm, actor: str, store: Any = None,
) -> Optional[Dict[str, Any]]:
    """按快照回写旧值；不存在/过期=None（路由回 404 文案）。

    快照带 ``conv_olds``（存量档位对齐明细）且 store 可用时，会话档位逐行
    回到对齐前的档（source=assistant_undo——诚实标注这行是撤销写的，不伪造
    原 source）；store 缺席时 overlay 键照撤、``conv_restored=0`` 如实回传。
    """
    with _LOCK:
        _gc(_UNDOS, UNDO_TTL_SEC)
        ent = _UNDOS.pop(str(undo_id or ""), None)
    if not ent:
        return None
    restored: List[str] = []
    failed: List[Dict[str, str]] = []
    for item in ent.get("olds", []):
        try:
            ok, msg = cm.set_overlay_flag(item["path"], item["old"])
        except Exception as ex:
            ok, msg = False, str(ex)
        if ok:
            restored.append(item["path"])
            _audit(cm, {"op": "undo", "actor": actor,
                        "action": ent.get("action"), "path": item["path"],
                        "restored": item["old"]})
        else:
            failed.append({"path": item["path"], "msg": str(msg or "")})
    conv_restored = 0
    conv_olds = ent.get("conv_olds") or []
    if store is not None and conv_olds:
        for o in conv_olds:
            try:
                try:
                    store.set_automation_mode(
                        o["cid"], o["mode"], source="assistant_undo")
                except TypeError:
                    store.set_automation_mode(o["cid"], o["mode"])
                conv_restored += 1
            except Exception:
                continue
        if conv_restored:
            _audit(cm, {"op": "align_modes", "actor": actor,
                        "action": ent.get("action"), "target": "(undo)",
                        "n": conv_restored})
    return {"ok": bool(restored), "restored": restored, "failed": failed,
            "action": ent.get("action"), "conv_restored": conv_restored}
