"""种子产品开关的「升级可达性」门禁——104 事故的**类别级**收口（2026-07-31）。

种子 ``config.desktop.min.yaml`` 只在**首次安装**播种（``ConfigManager._ensure_seeded``
仅当配置文件不存在时拷贝），于是**升级安装**的 config 是旧快照，种子里后加的开关对
存量用户等于不存在。目前有两套机制把「产品该默认开什么」从种子回归代码默认，使升级
安装也能拿到：

- **读时解析**：``platform_login.resolve_login_switch`` + ``_DESKTOP_LOGIN_DEFAULT_ON``
  （接入开关；不写用户配置）；
- **启动补齐**：``feature_registry`` A 类 + ``ConfigManager._ensure_baseline``
  （其余基线功能；把缺失键补进 overlay）。

已有门禁各守一个方向：

- ``test_desktop_seed_visibility``：注册表 A 类 → 种子必须开（**声明的必须落地**）；
- ``test_platform_login_defaults``：表 ↔ 种子双向 + 读取接线 + 字面直读旁路守卫。

**本文件守剩下那个方向**：种子里为 ``true`` 的每个开关，都必须被某套机制覆盖，
或落在带理由的豁免/待决策表里。没有它，「往种子加一个开关、但两套机制都不管」会
**静默通过全部现有门禁**——而那正是 104 的原始形态（种子开了 line/wa/messenger，
代码默认却全关，升级安装四平台扫码永久灰）。种子刻意保持最小，故两张表天然有界。

覆盖集**从两套机制的数据结构派生**（不手抄）：注册表加 A 类功能、默认表加平台键，
本门禁自动跟随——手工维护的清单正是 messenger/telegram 两次漏接的病根。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from src.integrations.platform_login import _DESKTOP_LOGIN_DEFAULT_ON
from src.utils.feature_registry import product_baseline_map

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"

#: 无需机制覆盖的种子开关 → 理由（每条都已核实到具体代码位置）。
#: 判据＝「升级安装天然也会开」：代码默认本就是 True，或另有等效的桌面默认机制。
_EXEMPT: Dict[str, str] = {
    "web_admin.enabled":
        "桌面态强制开：ConfigManager 的环境覆盖显式写 "
        "「AITR_DESKTOP_MODE：强制 web_admin.enabled=true」，不依赖配置键",
    "inbox.enabled":
        "代码默认已 True：bootstrap/web_app.py 挂持久层处取 "
        "_inbox_cfg.get('enabled', True)，升级安装天然不缺",
    "platform_login.enabled":
        "接入总闸代码默认已 True：登录路由 _platform_login_enabled 与 "
        "platform_readiness._login_enabled 均取 get('enabled', True)",
    "licensing.trial.enabled":
        "自带同款桌面默认机制（local_trial.configure_local_trial：桌面态且配置"
        "「从未写过」licensing.trial 时按桌面默认开）——本类修法的最早先例",
    "licensing.trial.enforce":
        "同上机制（local_trial.configure_local_trial：『enforce 没写过 → 桌面默认』，"
        "2026-08-11 起桌面默认 true——「注册领 100 万」链路上线后用尽即拦才有意义；"
        "显式 enforce:false 仍完全尊重）。升级安装不写该键也按桌面默认拿到，"
        "种子显式写 true 只为交付承诺文档化",
    "companion.proactive_topic.cold_start.enabled":
        "代码默认已 True：outbound_gate._DEFAULTS['enabled']=True，且 "
        "resolve_cold_start_cfg 在配置整段缺失时（None/{}/无 cold_start 节）一律回落"
        "该默认——这是刻意设计：冷启动隔离是**安全 floor**，不能取决于某份 overlay "
        "有没有提到它（.198 事故现场的 config.local 就完全没提主动触达的任何护栏）。"
        "种子里显式写出只为把交付承诺文档化，升级安装不写也照样生效",
    "companion.proactive_topic.cold_start.require_inbound_since_connect":
        "同上，outbound_gate._DEFAULTS['require_inbound_since_connect']=True",
    "companion.proactive_topic.cold_start.quota_gate":
        "同上，outbound_gate._DEFAULTS['quota_gate']=True",
    "inbox.read_from_store":
        "代码默认已 True（unified_inbox_aggregate._read_from_store_enabled，"
        "2026-07-31 由 False 改齐）：灰度早已完成，example/种子/生产均显式 true，"
        "原 False 默认只有陈旧升级配置会走到，正是那条静默分叉的成因",
    "inbox.l2_autosend.deliver_delay.adaptive":
        "非功能开关而是节奏子项（2026-08-07 出厂拟人默认随 min/max 一并播种，修"
        "「装完即秒回」）：升级可达路径＝设置页 /reply-settings「回复节奏」的"
        "「按内容长度自适应」复选框（模板 rps-adaptive，键同名，保存即热更），"
        "关掉后随时可再勾回；code 默认 resolve_pacing 取 get('adaptive', False)，"
        "种子写 true 只为出厂即用更拟人的长度自适应节奏，升级安装不写也不影响",
    # ── 全自动开箱四件套（2026-08-22 拍板，全自动一键化 P2）────────────────
    "inbox.l2_autosend.enabled":
        "代码默认已 True：bootstrap/web_app.py 装配 worker 处取 "
        "_as_cfg.get('enabled', True)，升级安装天然不缺",
    # inbox.auto_draft.bootstrap_automation_mode：1.0.76 D-M1（M-2 B）种子改 false
    # （登录默认半自动，全自动按账号显式开），不再是 true 开关，条目按本文件规则移除。
    "inbox.l2_autosend.deliver":
        "**刻意不静默补齐**（与 A 类基线机制的关键区别）：把正在人审运行的存量"
        "部署无声翻成全自动不可接受。升级可达路径＝①收件箱主管首开的一次性提示"
        "弹层（unified_inbox._maybeMasterAutoPrompt → 一键开启）②/reply-settings"
        "「AI 接管」三档主控 ③收件箱「AI 值守」胶囊——三个入口共用 POST "
        "/api/companion/standby 捆绑写入（含 worker 热接线，免重启生效）。"
        "种子 true 只翻新装（feature_registry 已 C→B 配套拍板）",
    "companion_send_gate.enabled":
        "安全闸随真发同批武装：新装随种子开；存量升级在用户接受上述任一入口的"
        "「全自动」时由 standby watching 捆绑计划一并写 true（capability_presets"
        "._priority 保证闸先立、deliver 后武装）——存量不开真发就不需要它，"
        "刻意不单独补齐",
}

#: **真实缺口，待产品决策**（沿用本仓 _PENDING_* 惯例：CI 保绿 + 债务可见 + 防过期）。
#: 与豁免的区别：这些确实存在「全新装开 / 升级装关」的行为分叉，只是「该不该给存量
#: 用户改」不是门禁能替产品定的。
#:
#: （首版唯一条目 ``inbox.read_from_store`` 已在 2026-07-31 按「代码默认对齐
#: 既有部署」收口，见 _EXEMPT 同名条目。）
_PENDING: Dict[str, str] = {
    "inbox.takeover_rearm.enabled":
        "接管自动接回（2026-08-09 .198/.104 事故沉淀）：新装机随种子开（桌面版"
        "「全自动」交付承诺闭环），升级安装保持代码默认关（takeover_rearm."
        "takeover_rearm_cfg 取 get('enabled', False)）。要不要经 feature_registry"
        " A 类给存量用户补齐＝产品决策：自动接回会覆盖坐席的隐式接管，对已习惯"
        "「发一条就永久转人工」的存量团队属行为变更，不宜门禁代拍。UI 侧不受影响："
        "让位横幅+一键接回对新旧安装都可用（不依赖本开关）。",
}


def _seed_cfg() -> dict:
    cfg = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict) and cfg, "桌面种子必须是非空 YAML mapping"
    return cfg


def _true_bool_leaves(node: Any, prefix: str = "") -> List[str]:
    """种子里所有取值为 ``True`` 的布尔叶子点分路径。

    只认真正的 ``bool`` True（不认 truthy 字符串/数字）——本门禁管的是「开关」，
    端口号/令牌/模型名之类不在语义内。列表不递归：种子里的列表是 intents/modes
    这类字符串枚举，不含开关。
    """
    out: List[str] = []
    if not isinstance(node, dict):
        return out
    for key, val in node.items():
        path = f"{prefix}{key}"
        if isinstance(val, bool):
            if val is True:
                out.append(path)
        elif isinstance(val, dict):
            out.extend(_true_bool_leaves(val, prefix=f"{path}."))
    return out


def _covered_by_mechanisms() -> Tuple[set, set]:
    """(读时解析覆盖集, 启动补齐覆盖集)——均从机制自身的数据结构派生。"""
    return set(_DESKTOP_LOGIN_DEFAULT_ON), set(product_baseline_map())


# ── 主不变量：种子里开着的每个开关都必须有归宿 ────────────────────────────────

def test_every_seed_switch_is_upgrade_reachable():
    resolve_set, baseline_set = _covered_by_mechanisms()
    accounted = resolve_set | baseline_set | set(_EXEMPT) | set(_PENDING)
    orphan = [p for p in _true_bool_leaves(_seed_cfg()) if p not in accounted]
    assert not orphan, (
        "种子打开了开关，但两套桌面默认机制都不管它——升级安装拿不到（104 原始形态）：\n"
        + "\n".join(f"  {p}" for p in orphan)
        + "\n\n四条出路，按语义选一条：\n"
          "  1. 接入类开关 → 加入 platform_login._DESKTOP_LOGIN_DEFAULT_ON 并把读取函数"
          "接上 resolve_login_switch；\n"
          "  2. 其余基线功能 → 进 feature_registry A 类（baseline=True），"
          "由 _ensure_baseline 启动补齐；\n"
          "  3. 代码默认本就为 True / 另有等效机制 → 加入本文件 _EXEMPT 并写明核实到的代码位置；\n"
          "  4. 确属真缺口但需产品拍板 → 加入 _PENDING 并写明分叉后果。")


# ── 两张表的防过期（守卫不许悄悄放空） ───────────────────────────────────────

def test_exempt_entries_still_in_seed():
    """豁免项必须**仍在种子里且为 true**——否则该条已无意义，应清理，
    免得表越攒越大、日后没人分得清哪条还成立。"""
    cfg = _seed_cfg()
    live = set(_true_bool_leaves(cfg))
    stale = sorted(set(_EXEMPT) - live)
    assert not stale, (
        "_EXEMPT 里的条目已不在种子里（或已不为 true），请删除以保持表精简：\n"
        + "\n".join(f"  {p}" for p in stale))


def test_pending_entries_still_uncovered():
    """待决策项必须**确实还没被任何机制覆盖**。

    一旦有人把它接进机制（正确的收口），这条会红，逼着从 _PENDING 移除——
    防「问题早修好了、债务表却还挂着」这种过期噪音（同 _PENDING_ORPHANS 惯例）。
    """
    resolve_set, baseline_set = _covered_by_mechanisms()
    covered = resolve_set | baseline_set
    fixed = sorted(set(_PENDING) & covered)
    assert not fixed, (
        "_PENDING 里的条目已被机制覆盖（问题已收口），请从 _PENDING 移除：\n"
        + "\n".join(f"  {p}" for p in fixed))


def test_exempt_and_pending_disjoint():
    overlap = sorted(set(_EXEMPT) & set(_PENDING))
    assert not overlap, f"同一键不能既豁免又待决策：{overlap}"


def test_mechanism_sets_are_non_empty():
    """两套机制的覆盖集都不得为空——空了说明表被误清，主断言会退化成恒真。"""
    resolve_set, baseline_set = _covered_by_mechanisms()
    assert resolve_set, "_DESKTOP_LOGIN_DEFAULT_ON 为空（读时解析机制失效）"
    assert baseline_set, "feature_registry A 类为空（启动补齐机制失效）"


# ── 探测器有效性自证 ─────────────────────────────────────────────────────────

def test_detector_catches_an_uncovered_switch():
    """往种子副本里塞一个两套机制都不管的开关，主断言的判定必须点名它——
    证明上面那条绿不是「扫了个寂寞」。"""
    cfg = _seed_cfg()
    cfg.setdefault("some_new_subsystem", {})["enabled"] = True
    resolve_set, baseline_set = _covered_by_mechanisms()
    accounted = resolve_set | baseline_set | set(_EXEMPT) | set(_PENDING)
    orphan = [p for p in _true_bool_leaves(cfg) if p not in accounted]
    assert orphan == ["some_new_subsystem.enabled"], \
        f"探测器未能识别未覆盖开关：{orphan}"
