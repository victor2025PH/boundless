"""产品功能交付注册表（P1，单一事实源）。

「演示机功能在安装包里全都看不见」事故（2026-07-31，工作目标为实锤标本）的
机制化收口：功能代码 100% 随包，但开关默认关 + 演示机靠不随包的 overlay 打开
+ 关闭即整卡隐藏 + 无自助入口。修法不是把开关都打开（会撞「开了但依赖没随包」
的更糟形态），而是把「哪个功能按什么档交付」收成一张表，四个消费面同源：

- 桌面种子门禁 ``tests/test_desktop_seed_visibility.py``——A 类在种子里必须开、
  C 类禁入种子（防整段拷 overlay）；
- ``ConfigManager._ensure_baseline``——桌面态启动增量补齐（存量安装升级也能
  拿到新 A 类，修「种子只影响新装」缺口）；
- ``/api/setup/features`` 功能总览（settings 页卡片）——B/C 类从「隐身」变
  「可见可开 / 可见并说明缺什么 / 可见并说明为什么未开放」；
- ``docs/安装包功能交付分级_2026-07.md``——本表的人话版（判据阐述 + 待拍板）。

分级判据：
- A 产品基线＝纯软件（本地 DB + 随包 AI 链即可跑、零 LAN/GPU 依赖、无合规风险）
  → 进种子默认开 + 启动补齐；
- B 可解锁＝默认关、客户可自助开。**允许零依赖的 B**（P2 起）：A vs B 的分界是
  「要不要默认开」这个产品决策，不是依赖结构（deep_persona/bubbles 曾是零依赖 B，
  2026-07-31 拍板升 A——升级只改 cls+baseline，配套不变量自动跟随）；
  **A 类硬性不变量**＝不得声明依赖、不得归售卖档（基线=无条件人人标配）；
- C 禁入种子＝绑本机集群 / 账号风险行为 / 待产品拍板
  → 绝不进种子；总览里只读展示原因（show=False 的仅作种子门禁，不进 UI）。

授权分层（P2）：``gate_feature`` 引用 ``src/licensing/feature_gate.FEATURE_MIN_PLAN``
的 family 键（如 voice_clone/companion/bazi/translation_suite）——**档位映射的
单一事实源留在 licensing 模块**（其自身注释即在防双源漂移），本表只挂引用；
空串=暂未归入任何售卖 family（goals/memvec/bubbles 属拍板项），gate 恒放行。
消费方（功能总览路由）在 gate 开启且档位不够时把 available/needs_dep 覆盖为
needs_upgrade（可见的锁）；已开启的功能永远如实报 on——运行时强制是
feature_gate 接线点的职责，总览只负责诚实。

纯函数、零框架依赖（licensing 引用只存字符串，不 import）；状态判定只读
config dict，不打网络（在线探活属 readiness 体系，别混进来——这里的「缺依赖」
指「配置层面没配」，确定性可测）。键路径必须与 config.example.yaml 或消费代码
逐字核实过才允许入表（bio_retrieval 核实结果＝**没有 enabled 总闸**只有调参键，
故不入表；accounts.profile_push 等桌面形态未评估，仍在台账候选）。

基线三条红线（老板决策 D-Q1 / D-Q2，2026-09-09，#267 #265；``_validate`` 与
``tests/test_baseline_redlines_q4.py`` 钉住）——1.0.78 把 ``inbox.work_schedule``
08:20–01:00 按**服务器本机时区**补进每台桌面机的 overlay，美英客户的下午被静默
关掉（KYHGSZ：Mike 01:46 来信、稿 01:47 生成、无日志暂存到 08:00）；额度闸门
「300 · 安装包默认」连人工也拦（CSTCJT）。事故沉淀为三条硬规则：

① **只补缺席键、绝不覆盖显式值**——``baseline_patch`` 判据是 ``dig()==None``，
   显式 false / 空列表 / 任何值都是用户表态；任何迁移都不得改写 overlay 里已存在
   的键（``ConfigManager.load`` 升级回读 ``[upgrade] user_flags_preserved`` 兜底）。
② **会改变发送行为的开关不得 ``baseline=True``**（A 类）——班表 / 额度闸门 /
   拆条 / 关怀真发 / 全自动真发。这些键只能是 B/C：默认关、用户自助开、开启时把
   「需要人决定的参数」（时区、额度）一并要求填。``SEND_BEHAVIOR_KEY_PREFIXES``
   是机器可判的名单，A 类命中即 fail-fast。
③ **时区类键不得默认服务器本机**——``*.timezone`` 键不得进 A 类基线，也不得以
   空串 / ``local`` 作 baseline；班表开启时时区必填（账号 / 客户 / 本机三选一，
   由用户显式选），``work_hours_gate`` 的「空＝本机钟」只对存量显式配置保留。
存量机器的撤回只对「由基线补进去的」值生效：``ConfigManager.baseline_rollback_patch``
按 1.0.78 基线原样形态识别（``enabled:true + 08:20/01:00`` 且无时区/账号覆写），
命中才回滚并落日志 ``[baseline] rollback <key> reason=D-Qn``；用户自己开过的一字不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

_VALID_CLS = ("A", "B", "C")

#: 红线②：会改变发送行为的开关前缀——命中的键**不得**是 A 类基线（fail-fast）。
#: 名单是「会让 AI 多发 / 少发 / 换时间发」的总闸与其参数块，不是全部 inbox 键。
SEND_BEHAVIOR_KEY_PREFIXES: Tuple[str, ...] = (
    "inbox.work_schedule.",            # 班表：夜间不回 / 复班补发（D-Q1 撤回 D-O4）
    "companion_send_gate.",            # 账号日发额度闸门（D-Q2 出厂关）
    "inbox.reply_style.bubbles.",      # 拆条（D-L3 已出厂关）
    "companion.proactive_care.",       # 关怀真发 / dry_run
    "companion.proactive_topic.",      # 主动话题外呼
    "inbox.l2_autosend.deliver",       # 全自动真发总闸
    "inbox.auto_draft.automation_mode",  # 全局档位（全自动）
    "inbox.auto_draft.bootstrap_automation_mode",
)

#: 红线③：时区类键的识别标记（点分路径任一段等于这些词即算时区键）。
TIMEZONE_KEY_MARKERS: Tuple[str, ...] = ("timezone", "tz")

#: 红线③：时区键禁止的 baseline 值（＝「服务器本机钟」的各种写法）。
_LOCAL_TZ_SENTINELS = ("", "local", "server", "system", None)

#: 1.0.78 基线补进去、1.0.79 撤回的键（D-Q1 / D-Q2）。value=1.0.78 基线写入的形态
#: （``ConfigManager.baseline_rollback_patch`` 用它做「来源是基线」的识别）。
ROLLED_BACK_BASELINES: Dict[str, Dict[str, Any]] = {
    "inbox.work_schedule.enabled": {
        "reason": "D-Q1", "old": True, "new": False,
        "shape_root": "inbox.work_schedule",
        "shape": {"enabled": True, "default": {"start": "08:20", "end": "01:00"}},
        "marker": "inbox.work_schedule.baseline_rollback",
    },
    "companion_send_gate.enabled": {
        "reason": "D-Q2", "old": True, "new": False,
        "shape_root": "companion_send_gate",
        "shape": {"enabled": True, "target_cap": 300,
                  "warmup_start_cap": 100, "warmup_ramp_days": 3},
        "marker": "companion_send_gate.baseline_rollback",
    },
}

#: 班表**开启时**的建议班次（不再是基线值；设置页做预填与换算提示用）。
WORK_SCHEDULE_SUGGESTED_SHIFT: Dict[str, str] = {"start": "08:20", "end": "01:00"}

#: Q-8 E / D-Q5（#264 2026-09-10）：按**业务域**解释「键缺席」的 B 类默认值。
#: 与 A 类 baseline 的差别：不补齐、不落 overlay、不进种子门禁——只在运行时读取处
#: （``effective_flag``）把 None 解释成域默认；用户显式写过的值永远优先。
#: 只允许 B 类键入表（A 类有 baseline 不需要；C 类禁入种子也不该被域默认打开）。
DOMAIN_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "companion": {
        "companion.proactive_topic.enabled": True,
    },
}


def domain_default(key: str, domain: str) -> Any:
    """``DOMAIN_DEFAULTS[domain][key]``；无 → None。"""
    return (DOMAIN_DEFAULTS.get(str(domain or "").strip().lower()) or {}).get(str(key or ""))


def effective_flag(cfg: Any, key: str, domain: str, fallback: Any = None) -> Any:
    """配置里显式值优先；缺席 → 域默认 → ``fallback``。运行时读开关的统一口。"""
    v = dig(cfg, key)
    if v is not None:
        return v
    d = domain_default(key, domain)
    return fallback if d is None else d


def is_send_behavior_key(key: str) -> bool:
    """红线②判定：键是否落在「会改变发送行为」名单（前缀或精确匹配）。"""
    k = str(key or "")
    for p in SEND_BEHAVIOR_KEY_PREFIXES:
        if p.endswith("."):
            if k.startswith(p):
                return True
        elif k == p or k.startswith(p + "."):
            return True
    return False


def is_timezone_key(key: str) -> bool:
    """红线③判定：点分路径任一段是时区词。"""
    parts = [s.lower() for s in str(key or "").split(".") if s]
    return any(p in TIMEZONE_KEY_MARKERS for p in parts)


@dataclass(frozen=True)
class Feature:
    key: str                 # 开关的点分 config 路径（与 example.yaml 逐字核实）
    cls: str                 # "A" 基线 | "B" 可解锁 | "C" 禁入种子
    slug: str                # i18n 短名：fc_f_<slug> / fc_f_<slug>_d
    note: str                # 内部备注（门禁红时的人话理由；不对用户显示）
    baseline: Any = None     # A 类：种子/补齐写入值
    requires: Tuple[str, ...] = ()   # B 类：依赖码（见 DEP_CHECKERS）
    reason: str = ""         # C 类：锁定原因码（fc_rsn_<reason>）
    show: bool = True        # 是否进功能总览 UI（False=仅作种子门禁）
    gate_feature: str = ""   # 授权 family（feature_gate.FEATURE_MIN_PLAN 键；空=不归档位）


def dig(cfg: Any, dotted: str) -> Any:
    """点分路径取值；任何一层缺失/非 dict → None。"""
    cur = cfg
    for part in str(dotted or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _has_embedding(cfg: dict) -> bool:
    ai = cfg.get("ai") or {}
    if not isinstance(ai, dict):
        return False
    urls = ai.get("embedding_base_urls")
    if isinstance(urls, (list, tuple)) and any(str(u or "").strip() for u in urls):
        return True
    return bool(str(ai.get("embedding_base_url") or "").strip())


def _engine_order(cfg: dict) -> List[str]:
    order = dig(cfg, "translation.engines.order")
    if isinstance(order, (list, tuple)):
        return [str(x) for x in order if str(x or "").strip()]
    return []


#: 依赖码 → 判定函数（只读 config，确定性；label 走 i18n fc_dep_<code>）
DEP_CHECKERS: Dict[str, Callable[[dict], bool]] = {
    "embedding": _has_embedding,
    "translation_multi": lambda cfg: len(_engine_order(cfg)) >= 2,
    "translation_any": lambda cfg: len(_engine_order(cfg)) >= 1,
}


FEATURES: Tuple[Feature, ...] = (
    # ── A 类：产品基线（人人标配——不得声明依赖、不得归售卖档） ──────────────
    Feature(
        key="companion.goals.enabled", cls="A", slug="goals", baseline=True,
        note="工作目标：右栏卡 + prompt 注入 + /api/goals*；目标库落用户数据区，"
             "规划走随包 AI 链，零 LAN 依赖"),
    Feature(
        key="companion.deep_persona.enabled", cls="A", slug="deep_persona",
        baseline=True,
        note="深度人设分层注入：master flag 见 ai_client（各层软降级，零硬依赖）；"
             "2026-07-31 拍板升 A"),
    Feature(
        key="avatar_voice._hosted_auto", cls="A", slug="hosted_voice_auto",
        baseline=True, show=False,
        note="托管语音预授权标记（2026-08-19 报障群实测「客户与语音引擎零通路」）："
             "仅是标记非开关——enabled 仍静态保守关（avatar_voice.enabled C 类语义"
             "不变），持设备令牌（cx.*）的部署由 hosted_gateway 运行时纯内存自动"
             "接入官方语音网关；用户退出走 avatar_voice.hosted_opt_out。A 类补齐"
             "让存量安装升级后零操作获得托管语音（修「种子只影响新装」）"),
    Feature(
        key="voice_recognition._hosted_auto", cls="A", slug="hosted_asr_auto",
        baseline=True, show=False,
        note="托管语音识别（转写）预授权标记（2026-08-20 内测群实测「对方语音消息"
             "无法识别」——本地 ASR 依赖被打包排除、客户部署与转写引擎零通路）："
             "与 avatar_voice._hosted_auto 同构，持设备令牌的部署由 ensure_hosted_asr"
             "运行时纯内存接入官网网关转写；用户退出走 voice_recognition."
             "hosted_opt_out。A 类补齐让存量安装升级后零操作恢复语音识别"),
    Feature(
        key="vision._hosted_auto", cls="A", slug="hosted_vision_auto",
        baseline=True, show=False,
        note="托管识图预授权标记（2026-08-22 外网机实测「发图即拦、顶栏报 AI 不能"
             "自动回」——cloud_light 把 vision.base_urls 置空且 enabled 出厂关，"
             "ensure_hosted_vision 只 setdefault 不扳回显式 false → 网关从未被打）："
             "与 avatar_voice._hosted_auto 同构，持设备令牌的部署由 ensure_hosted_vision"
             "运行时纯内存接入官网识图网关（集群 GPU VLM）；用户退出走 vision."
             "hosted_opt_out。A 类补齐让存量安装升级后零操作获得识图"),
    Feature(
        key="inbox.auto_draft.media_degrade_reply", cls="A",
        slug="media_degrade_reply", baseline=True, show=False,
        note="识别失败降级诚实自动回（2026-08-22 老板「不要再有小问题掐断全自动」"
             "拍板，实施56 P1）：图片/语音识别失败时不再扣留整条回复，两条自动链"
             "放行生成——ai_client 媒体块对无描述媒体自带「自然承认收到+温和追问」"
             "话术，诚实降级绝不装懂。与 08-17 无兜底纪律的和解＝**代码默认 False**"
             "（服务器实例维持拦下），客户桌面包经种子/A 类基线默认 True。"
             "show=False：行为策略非功能卡，判定单点 media_enrich."
             "media_degrade_reply_enabled"),
    Feature(
        key="companion.goals.notify.enabled", cls="A", slug="goals_notify",
        baseline=True, show=False,
        note="工作目标完成提醒扫描（2026-08-20 内测群实测：右栏目标卡对用户显示"
             "「🔕 完成提醒未开启（companion.goals.notify）」——提示甩了一个用户"
             "碰不到的 config 键，而该功能纯软件零依赖（EventBus 应用内提醒），"
             "按种子哲学应出厂即开）。goals 本体已是 A 类，本键是其提醒子开关；"
             "show=False：UI 面就是目标卡自身，不在功能总览重复列行"),
    Feature(
        key="assistant.enabled", cls="A", slug="assistant", baseline=True,
        note="小智 AI 助手悬浮球（产品问答/一键报障/我的工单/教学模式/替我做智能体）："
             "帮助语料库随代码生成并首启自动播种（src/assistant/seed_corpus），问答 LLM "
             "走主链托管网关容灾，报障写本地 bug_tickets——零 LAN 硬依赖。2026-08-23 "
             "老板拍板进基线（1.0.50 发布说明宣传了小智、包里开关却是关的＝发布说明与"
             "交付配置脱节事故；A 类补齐让存量安装升级后零操作点亮）。子开关 voice "
             "（依赖 GPU ASR 端点）刻意不进基线，由内测种子显式开"),
    Feature(
        key="assistant.agent.enabled", cls="A", slug="assistant_agent",
        baseline=True, show=False,
        note="小智「替我做」智能体：动作层全部映射既有白名单写口（settings/"
             "reply-settings/features toggle…），两段式确认卡+可撤销+审计，纯软件。"
             "show=False：UI 面就是小智面板自身，功能总览由 assistant 主行代表"),
    Feature(
        key="assistant.vision.enabled", cls="A", slug="assistant_vision",
        baseline=True, show=False,
        note="小智报障截图 VLM 摘要：best-effort（VLM/托管识图掉线静默，绝不影响"
             "落单）——软依赖不构成 A 类阻断。show=False 同上"),
    Feature(
        key="inbox.outbound_dup_guard.enabled", cls="A", slug="out_dup_guard",
        baseline=True, show=False,
        note="出站近重复守卫（2026-08-22 随全自动开箱进基线，B41「同稿双投」防线"
             "的 DB 镜像层——唯一跨重启的同义双发拦截）：投递前与最近出站比对，"
             "命中静默跳过。纯软件零依赖、只减发送不增，方向性安全；zhiliao 已"
             "灰度数周。show=False：内部守卫无用户操作面"),
    Feature(
        key="inbox.l2_autosend.fresh_guard.enabled", cls="A",
        slug="fresh_guard", baseline=True, show=False,
        note="新入站过期守卫（2026-08-22 随全自动开箱进基线）：拟稿/拟人延迟窗内"
             "客户又说话 → 旧稿作废等新稿覆盖两问，防「答非所问+两连发」。"
             "纯软件零依赖、只减发送不增；zhiliao 已灰度数周。show=False 同上"),
    Feature(
        key="telegram.poll_fallback.mirror_outgoing", cls="A",
        slug="tg_out_mirror", baseline=True, show=False,
        note="TG 出站镜像（2026-08-20 内测群实测「主动给客户发消息，左侧没有客户"
             "聊天窗口」——镜像默认关 → 纯出站会话永不落收件箱，用户以为丢会话）："
             "统一收件箱产品的用户理应看到自己发起的会话；镜像文本占位随既有轮询"
             "零额外下载 RPC、direction=out 永不触发自动回复、MTProto id 去重防双"
             "气泡、仅私聊。媒体本体归档（mirror_outgoing_media，有下载 RPC 风控"
             "面）刻意不进基线保持默认关"),
    # ── 跨平台联系人「精简档」（2026-08-20 内测群实测：右栏「跨平台档案」卡对
    # 客户报 contacts_disabled 裸错误码——功能整条链随包，只是总开关默认关）。
    # 拍板结果（实施49 P0-2）＝**开精简档**而非二选一：本地 contacts.db + 跨平台
    # 档案读写/AI 注入是纯软件（零 LAN/GPU、零账号风险），而这个子系统真正的重
    # 运行时（衰减/KPI 周期任务、RPA hooks 逐条记账、Mobile Bridge 每 15s 打本机
    # 18080 手机 rig）全部由 contacts.mode=lite 显式不启动——见 contacts/bootstrap。
    # ⚠ 两键必须成对：只补 enabled 不补 mode，存量安装会被基线补齐补出 full 档
    # （resolve_contacts_mode 缺失即 full，这是为不惊动生产/内测坐席刻意选的默认）。
    Feature(
        key="contacts.enabled", cls="A", slug="contacts", baseline=True,
        show=False,
        note="跨平台联系人库（本地 SQLite）+ 跨平台档案卡：客户端按精简档交付，"
             "只起 store + origin_profile 读写/AI 注入"),
    Feature(
        key="contacts.mode", cls="A", slug="contacts_mode", baseline="lite",
        show=False,
        note="contacts 运行档位：客户端 lite（不起周期任务/RPA hooks/Mobile "
             "Bridge）；运营/内测坐席在 config.desktop.internal.yaml 显式 full"),
    Feature(
        key="contacts.origin_profile.enabled", cls="A", slug="origin_profile",
        baseline=True, show=False,
        note="跨平台档案（来源平台/那边的昵称/聊过的话题域 → _origin_block 注入）："
             "纯本地表 + 随包 AI 链，坐席右栏卡的后端；关着就只剩一张报错的卡"),
    # ── 老板决策 D1 / D7（2026-09-05）→ D-L1「出厂开进基础配置，客户版也开」
    # （2026-09-06 L-5）。此前两项只写在 cloud_light（仅全新安装播种）与内测种子
    # config.desktop.internal.yaml（只进 smart 包）里：两台内测机走应用内更新拿的
    # 是 clean 包 + 升级安装 → 两条播种路都不经过，1.0.74 启动仍 sprint.enabled=False、
    # 危机留痕关。进 A 类基线 = _ensure_baseline 在**存量安装**首启补进 overlay
    # （合并视图缺键才补；用户显式 false 永远尊重），不再依赖装的是哪种包。
    Feature(
        key="companion.goals.sprint.enabled", cls="A", slug="goal_sprint",
        baseline=True, show=False,
        note="工作目标冲刺推进器（#166 D1）：限时目标选「自动推进」后按相位排主动拍，"
             "派发走 care 管线全套护栏（silence_min/min_gap/危机/opt-out/平台白名单/"
             "仅 auto_ai 会话）；纯软件零 LAN 依赖。show=False：UI 面是目标卡自身"),
    Feature(
        key="companion.wellbeing.crisis_audit", cls="A", slug="crisis_audit",
        baseline=True, show=False,
        note="客户安全预警留痕（#185 D7）：severe/elevated 危机事件落本地库短摘要"
             "（≤120 字），页面才有数据可看；隐私靠角色门不靠关闭。纯本地 SQLite"),
    Feature(
        key="companion.wellbeing.crisis_escalation", cls="A", slug="crisis_escalation",
        baseline=True, show=False,
        note="客户安全预警人工升级（#185 D7）：severe 命中点亮工作台「需人工」徽标 + "
             "置顶会话 + 落案例，不依赖外部 webhook；纯软件零依赖"),
    # ── 老板决策 D-O5（2026-09-08 O-2 A，WYNN22 #201）：情景记忆抽取白名单进基线。
    # 第三次「出厂默认没进基线」：should_extract_intent 按 memory.extract.intents 白名单
    # 放行、**空即不抽**（Phase D 为存量零回归刻意如此）；服务器 config.yaml 有四值、
    # 内测种子 config.desktop.internal.yaml 走 match_all（只进 smart 包）、而 clean 包
    # 播种的 config.desktop.min.yaml **没有任何 memory 块** → 每台全新安装的记忆从首日
    # 起为死（skuio 8 小时 30 次 schedule run 全 skip intents=[]，卡片却写「已启用」）。
    # 三键成组：enabled/use_llm 代码默认本就 True，进表只为「显式交付 + 缺键补齐」同源；
    # intents 是真正缺的那把钥匙——与服务器同值，不选 match_all（greeting/stop_contact
    # 噪音 + token 成本）。列表型 baseline：baseline_patch 逐次拷贝，防合并后被原地改。
    # 三态语义照旧：合并视图缺键才补；用户显式写 intents: [] ＝关掉，尊重（启动 WARNING）。
    Feature(
        key="memory.extract.enabled", cls="A", slug="mem_extract",
        baseline=True, show=False,
        note="情景记忆抽取总闸（D-O5 #201）：代码默认已 True，进基线为与 intents 同组"
             "显式交付；本地 SQLite + 随包 AI 链，零 LAN 依赖"),
    Feature(
        key="memory.extract.use_llm", cls="A", slug="mem_extract_llm",
        baseline=True, show=False,
        note="情景记忆 LLM 抽取（D-O5 #201）：启发式正则之外由主链 LLM 抽事实（引文级"
             "接地）；代码默认已 True，进基线为与 intents 同组显式交付"),
    Feature(
        key="memory.extract.intents", cls="A", slug="mem_extract_intents",
        baseline=["direct_chat", "small_talk", "greeting", "complaint"], show=False,
        note="情景记忆可抽取意图白名单（D-O5 #201，WYNN22 干净包首日即死）：与服务器"
             "config.yaml 同值；缺席＝should_extract_intent 恒 False＝记忆永不新增"),
    # ── 老板决策 D-O4（2026-09-08 O-1 D，FW78ZP #252 #254）：拟人节奏出厂基线 ──
    # 事故：起草→发出恒 12–13s 与长度无关、全天秒回、零 composing。旧 min/max/adaptive
    # 模型对 60 字英文只估 5.8s，被种子 min_sec=8 夹成常数。三档 profile 在场时 humanize
    # 走「读 / 想 / 打字」分量模型，min/max 只作 custom 回落值。**存量语义**：块里已显式
    # 写过 profile（含 custom）的一字不动；只有缺键的补 natural——正是那批「被夹成常数」
    # 的机器要换的行为。设置页「拟人程度」四档随时可改（custom＝回旧模型）。
    Feature(
        key="inbox.l2_autosend.deliver_delay.profile", cls="A", slug="pacing_profile",
        baseline="natural", show=False,
        note="拟人节奏「拟人程度」出厂档 natural（D-O4 #252 #254）：读 3–6s + 每 10 词 +1s、"
             "想 3–8s、打字 35–45 wpm / CJK 60–90 字/分 ±30%、发出前 1s 停 composing；"
             "缺键＝旧 min/max 模型（FW78ZP 恒 12–13s 的来源）"),
    # 客户连发合并等 8–15s 一次回：复用 inbound_merge（08-02 P0，内测种子已开、clean 种子
    # 没有）——不做第二套；窗口 8–15s 随机由 window_max_sec 缺省 15 带。
    Feature(
        key="inbox.auto_draft.inbound_merge.enabled", cls="A", slug="inbound_merge",
        baseline=True, show=False,
        note="入站爆发合并（D-O4 #252 #254）：客户几秒内连发多条 → 等 8–15s 静默窗合并"
             "只回一次；缺键＝每条各自拟稿各自回（同义双发 + 机关枪节奏）"),
    # ── Q-5 代（D-Q 2026-09-09，#267）：摸底目标 LLM 摘录补槽进基线。clean 包 goal
    # engine-status 恒报 profile_llm_off（service.py L1443），兴趣槽只能人工补录；纯软件、
    # 随包 AI 链、只多学不多发（不是发送行为开关，红线②不适用）。
    Feature(
        key="companion.goals.profile_llm.enabled", cls="A", slug="goals_profile_llm",
        baseline=True, show=False,
        note="摸底目标 LLM 摘录补槽（Q-5 代 #267）：正则轨盲区由主链 LLM 摘录客户自由表达"
             "填画像槽；clean 包此前恒 profile_llm_off，兴趣槽永远只能人工补录"),
    # ── 老板例外（2026-09-24，机器码 6117-777B 报「LINE 发不了媒体」）：LINE 出站媒体
    # 走逆向协议有封号风险、打开后 LINE 也进自拍/相册/语音投递——按三分法本应 C/B，
    # 老板知情拍板公共包默认开。代码默认仍 False（服务器实例不变），显式 false 永远尊重。
    Feature(
        key="platform_login.line.media.outbound", cls="A", slug="line_media_outbound",
        baseline=True, show=False,
        note="LINE 出站媒体（老板例外 2026-09-24）：缺键＝LineProtocolWorker 不挂 "
             "send_media，工作台/AI 报 no_send_media；内测种子早已显式开，公共包缺失。"
             "A 类补齐让存量安装升级后零操作获得 LINE 发图/语音"),
    # ── B 类：可解锁（依赖齐了可一键开；零依赖 B=未拍板进基线的纯软件功能） ──
    # 2026-09-09 老板决策 D-Q1（#267 KYHGSZ，撤回 D-O4）：班表 A→B **出厂关**。1.0.78 基线把
    # 08:20–01:00 + 「timezone 空＝本机钟」补进每台机器——桌面机在上海、客户在纽约，
    # 客户的下午＝我们的凌晨 → 自动回被整段静默扣留且无日志。红线②③双命中。开启入口＝
    # 自动回复设置页「工作时间」：时区必填（账号 / 客户 / 本机三选一），页面给换算提示。
    # 建议班次见 WORK_SCHEDULE_SUGGESTED_SHIFT（预填值，不是基线）。存量：由 1.0.78 基线
    # 补进去的原样形态由 ConfigManager.baseline_rollback_patch 回滚；用户自己开过的不动。
    Feature(
        key="inbox.work_schedule.enabled", cls="B", slug="work_schedule",
        show=False,
        note="账号作息班表总闸（D-Q1 #267 撤回 D-O4）：会改变发送时间的开关，出厂关；"
             "开启时时区必填，缺时区不得保存"),
    # 2026-09-09 老板决策 D-Q2（#267 CSTCJT）：账号日发额度闸门**出厂关**（种子 300/100/3
    # 曾显式 enabled:true，各号折算 114–129 且人工也拦）。B 类：默认关、设置页自助开；
    # 开启后只作用 origin=auto，人工发送永不受额度限制（companion_send_gate.gate_decision）。
    Feature(
        key="companion_send_gate.enabled", cls="B", slug="send_gate",
        show=False,
        note="单账号日发额度闸门（D-Q2 #267）：会减少发送的开关，出厂关；开启后只拦自动链，"
             "人工发送永不限制；「300 · 推荐」是建议值不是默认值"),
    # D-M9（1.0.76）：手动是粘的——坐席接管后 AI 全局自动接回**出厂关**（种子曾按「全自动」
    # 承诺显式开）。B 类：默认关；坐席切手动时可单独选「30 分钟后接回」（takeover_opt_from），
    # 会恢复自动发送的开关不得由基线替用户打开。
    Feature(
        key="inbox.takeover_rearm.enabled", cls="B", slug="takeover_rearm",
        show=False,
        note="坐席接管静默后 AI 全局自动接回（takeover_rearm.sweep_takeover_rearm）：会重新"
             "开启自动发送的开关，D-M9 出厂关；按会话的接回由坐席切手动时自选"),
    # 2026-09-06 老板决策 D-L3（#210，82BF95 付费客户的客户识破 AI）：拆条**出厂关**
    # ——2026-07-31 升 A 的「默认开安全」前提（真发仍逐条前端 opt-in）在 08 月三链
    # 自动拆条接线后已不成立；A 类基线曾让每台桌面机首启被 _ensure_baseline 补成
    # true（skuio 机 enabled=true 的来源）。降 B：默认关、客户可在自动回复设置页
    # 自助开（开后也只按草稿显式换行拆、短回复永不拆，见 reply_split）。
    Feature(
        key="inbox.reply_style.bubbles.enabled", cls="B", slug="bubbles",
        note="多句分条回复（reply_split.parse_bubbles_cfg 总闸）：零依赖纯软件；"
             "2026-09-06 D-L3 出厂关——拆条节奏可被客户用来识别 AI（82BF95）"),
    Feature(
        key="memory.vector.enabled", cls="B", slug="memvec",
        requires=("embedding",),
        note="向量记忆召回：需要 OpenAI 兼容嵌入端点（ai.embedding_base_url[s]）"),
    Feature(
        key="translation.engines.confidence_switch.enabled", cls="B",
        slug="xlate_conf", requires=("translation_multi",),
        gate_feature="translation_suite",
        note="翻译置信度智能切换：只有 ≥2 个引擎时切换才有意义"),
    Feature(
        key="inbox.l2_autosend.translate.enabled", cls="B", slug="out_xlate",
        requires=("translation_any",), gate_feature="translation_suite",
        note="出站自动翻译：投递前译成客户语言，需至少一个翻译引擎"),
    Feature(
        key="personas.quiz.enabled", cls="B", slug="quiz",
        gate_feature="personas",
        note="人设考题（LLM 逐题问答出质量报告）：随包 AI 链即可跑；"
             "/api/personas/ 前缀本就在 personas family 门控内，映射保持一致"),
    Feature(
        key="accounts.profile_push.enabled", cls="B", slug="profile_push",
        note="账号资料推送（昵称/签名/头像推到平台官方）：协议账号随包可用，"
             "写口自带冷却 + ops 审计；2026-07-31 P4 核实入表"),
    # ── C 类：禁入种子（show=True 的在总览里只读展示原因） ──────────────────
    Feature(
        key="avatar_voice.enabled", cls="C", slug="avatar_voice", reason="lan",
        gate_feature="voice_clone",
        note="AvatarHub TTS 是本机 LAN 集群（117/140），客户机不可达=语音静默回落"),
    Feature(
        key="companion.selfie.enabled", cls="C", slug="selfie", reason="lan",
        gate_feature="companion",
        note="出图后端是 LAN ComfyUI，相册备货属运营内容，均未随包"),
    Feature(
        # Q-8 E / D-Q5（#264 2026-09-10）：C(risk)→B。陪伴产品的「主动惦记」是产品本体
        # 不是风险附件（B9D8NW 等三包：AI 四轮纯夸赞后自己退场，没人主动带话题）。
        # 红线②禁 A：会改变发送行为的键不得**静默补齐**存量；于是不写 baseline，改走
        # ``DOMAIN_DEFAULTS``：键**缺席**且业务域=companion 时运行时按 True 解释（不落
        # overlay、用户显式 false 永远尊重、销售域仍默认关）。真发仍受 dry_run / 沉默时长 /
        # 冷却 / 停联 / risk_hold / 节奏 / 预算全部既有闸。
        key="companion.proactive_topic.enabled", cls="B", slug="proactive",
        gate_feature="companion",
        note="AI 主动惦记外呼：陪伴域缺席即开（DOMAIN_DEFAULTS，不静默改写配置）；"
             "销售域默认关；随 companion 档位售卖"),
    Feature(
        key="companion.bazi.enabled", cls="C", slug="bazi", reason="pending",
        gate_feature="bazi",
        note="命理内容合规敏感，交付档位待市场/法务拍板"),
    # ── C 类（仅种子门禁，不进 UI） ─────────────────────────────────────────
    Feature(
        key="realtime_voice.enabled", cls="C", slug="realtime_voice",
        reason="lan", show=False, note="实时语音依赖 LAN GPU 服务，未随包"),
    Feature(
        key="speech_emotion.enabled", cls="C", slug="speech_emotion",
        reason="lan", show=False,
        note="SER 主路是 176 GPU、本地回落 funasr 依赖 torch——torch 被打包显式排除"),
    Feature(
        key="ai.fallback.enabled", cls="C", slug="ai_fallback", reason="lan",
        show=False, note="主链兜底端点是 LAN Ollama（176），客户机白付超时"),
    Feature(
        key="ops.gpu_watermark.enabled", cls="C", slug="gpu_watermark",
        reason="lan", show=False,
        note="探 LAN Ollama /api/ps 的运维卡，客户环境恒 unknown"),
    Feature(
        key="ops.cloud_credentials.enabled", cls="C", slug="cloud_credentials",
        reason="lan", show=False,
        note="运营商自有 Key 余额巡检；托管版用户走官网网关、无自有 Key 可巡"),
    Feature(
        key="monetization.enabled", cls="C", slug="monetization",
        reason="pending", show=False,
        note="运营商向终端聊天用户收费的系统，需 entitlement resolver 配套，误开会闸权益"),
    Feature(
        key="line_rpa.enabled", cls="C", slug="line_rpa", reason="infra",
        show=False, note="需安卓真机/adb 基础设施，桌面包不带"),
    Feature(
        key="messenger_rpa.enabled", cls="C", slug="messenger_rpa",
        reason="infra", show=False, note="需安卓真机/adb 基础设施，桌面包不带"),
    Feature(
        key="whatsapp_rpa.enabled", cls="C", slug="whatsapp_rpa",
        reason="infra", show=False, note="需安卓真机/adb 基础设施，桌面包不带"),
    Feature(
        # 2026-08-22 拍板（全自动一键化 P2）：C(safety)→B——客户包出厂即全自动
        # （种子显式 deliver:true，B37 实录两位内测用户 100% 卡死在「装好后还要
        # 找到第二道开关」上）。安全职责不变、只是搬家：出站安全闸/回复额度守卫/
        # 每日额度/kill-switch 全部默认在岗，用户可在「AI 接管」三档一键退到
        # 拟稿人审。刻意不升 A：A 类会在存量安装升级时**静默补齐**——把正在人审
        # 运行的老部署无声翻成全自动是不可接受的（存量走一次性提示条自选）。
        key="inbox.l2_autosend.deliver", cls="B", slug="autosend_deliver",
        gate_feature="ai_autosend",
        note="AI 自动真发总闸：新装种子出厂即开（全自动开箱），存量升级不静默"
             "翻转（一次性提示条自选）；随 ai_autosend 档位售卖"),
)


def _validate() -> None:
    """入表即校验（fail fast，与 i18n packs 冲突检测同哲学）。"""
    seen_keys: set = set()
    seen_slugs: set = set()
    for f in FEATURES:
        if f.cls not in _VALID_CLS:
            raise ValueError(f"feature_registry: 非法分级 {f.cls!r}（{f.key}）")
        if f.key in seen_keys:
            raise ValueError(f"feature_registry: 重复键 {f.key!r}")
        if f.slug in seen_slugs:
            raise ValueError(f"feature_registry: 重复 slug {f.slug!r}")
        seen_keys.add(f.key)
        seen_slugs.add(f.slug)
        if f.cls == "A":
            if f.baseline is None:
                raise ValueError(f"feature_registry: A 类必须给 baseline 值（{f.key}）")
            if f.requires:
                raise ValueError(
                    f"feature_registry: A 类=无条件基线，不得声明依赖（{f.key}）"
                    "——有依赖的功能不配当基线，降回 B")
            if f.gate_feature:
                raise ValueError(
                    f"feature_registry: A 类人人标配，不得归售卖档（{f.key}）"
                    "——要按档卖就别放基线，降回 B/C")
            # 红线②（D-Q1/D-Q2 2026-09-09）：会改变发送行为的开关不得默认开
            if is_send_behavior_key(f.key):
                raise ValueError(
                    f"feature_registry: 红线②——会改变发送行为的开关不得进 A 类基线"
                    f"（{f.key}）；降回 B，让用户自助开并把时区/额度一并填")
            # 红线③：时区类键不得默认服务器本机（空 / local / server）
            if is_timezone_key(f.key):
                raise ValueError(
                    f"feature_registry: 红线③——时区类键不得进 A 类基线（{f.key}）"
                    "：时区只能由用户显式选（账号 / 客户 / 本机），不得默认服务器本机")
        if is_timezone_key(f.key) and f.baseline in _LOCAL_TZ_SENTINELS and f.baseline is not None:
            raise ValueError(
                f"feature_registry: 红线③——时区键 baseline 不得是本机哨兵值（{f.key}={f.baseline!r}）")
        if f.cls == "C" and not f.reason:
            raise ValueError(f"feature_registry: C 类必须给 reason 码（{f.key}）")
        for code in f.requires:
            if code not in DEP_CHECKERS:
                raise ValueError(
                    f"feature_registry: 未注册的依赖码 {code!r}（{f.key}）")
    # Q-8 E：域默认只许挂在 B 类键上（A 有 baseline；C 禁入种子不得被域默认打开）
    by = {f.key: f for f in FEATURES}
    for dom, kv in DOMAIN_DEFAULTS.items():
        for k in kv:
            f = by.get(k)
            if f is None or f.cls != "B":
                raise ValueError(
                    f"feature_registry: DOMAIN_DEFAULTS[{dom!r}] 只许 B 类键（{k} → "
                    f"{getattr(f, 'cls', '未入表')}）")


_validate()


def by_key(key: str) -> Optional[Feature]:
    for f in FEATURES:
        if f.key == key:
            return f
    return None


def product_baseline_map() -> Dict[str, str]:
    """A 类 {key: note}——种子门禁/文档用。"""
    return {f.key: f.note for f in FEATURES if f.cls == "A"}


def product_baseline_values() -> Dict[str, Any]:
    """A 类 {key: baseline}——种子门禁按**声明值**校验，而非一律按 True。

    2026-08-20 起 baseline 不再只有布尔（``contacts.mode="lite"``）：档位型基线
    同样要求「种子里必须是这个值」，写成别的值＝客户装机跑成另一档。
    """
    return {f.key: f.baseline for f in FEATURES if f.cls == "A"}


def seed_forbidden_map() -> Dict[str, str]:
    """C 类 {key: note}——种子门禁用（含 show=False 条目）。"""
    return {f.key: f.note for f in FEATURES if f.cls == "C"}


def baseline_patch(cfg: dict) -> Dict[str, Any]:
    """A 类里**合并视图缺失**的键 → {dotted: baseline}。

    判据刻意用「缺失」而非「非真」：缺失=用户从未表达过意见（补齐零冲突）；
    显式 false 是用户决定，永远尊重。
    """
    out: Dict[str, Any] = {}
    for f in FEATURES:
        if f.cls == "A" and dig(cfg, f.key) is None:
            # 列表型 baseline（memory.extract.intents）每次给新副本：patch 会被深合并进
            # 运行时 config 并落 overlay，共享同一个 list 对象＝谁改运行时谁就改了注册表。
            out[f.key] = list(f.baseline) if isinstance(f.baseline, list) else f.baseline
    return out


def as_nested(dotted_map: Dict[str, Any]) -> Dict[str, Any]:
    """{a.b.c: v} → {a: {b: {c: v}}}（供 save_overlay_patch 落 overlay）。"""
    root: Dict[str, Any] = {}
    for dotted, value in (dotted_map or {}).items():
        keys = [k for k in str(dotted).split(".") if k]
        if not keys:
            continue
        node = root
        for k in keys[:-1]:
            nxt = node.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                node[k] = nxt
            node = nxt
        node[keys[-1]] = value
    return root


def missing_deps(f: Feature, cfg: dict) -> List[str]:
    """未满足的依赖码列表（B 类以外恒空）。"""
    out: List[str] = []
    for code in f.requires:
        try:
            ok = bool(DEP_CHECKERS[code](cfg or {}))
        except Exception:
            ok = False
        if not ok:
            out.append(code)
    return out


def feature_state(f: Feature, cfg: dict, domain: str = "") -> str:
    """确定性状态：on | available | needs_dep | locked。

    已开启（不论分级）如实报 on——运营机上 C 类经 overlay 打开是合法部署形态，
    总览必须与真实行为一致，不装「未开放」。``domain`` 给了则键**缺席**时按
    ``DOMAIN_DEFAULTS`` 解释（Q-8 E：陪伴域 proactive_topic 缺席即 on）。
    """
    raw = dig(cfg, f.key)
    if bool(raw):
        return "on"
    if raw is None and domain and bool(domain_default(f.key, domain)):
        return "on"
    if f.cls == "C":
        return "locked"
    if missing_deps(f, cfg):
        return "needs_dep"
    return "available"


def ui_features() -> Tuple[Feature, ...]:
    return tuple(f for f in FEATURES if f.show)
