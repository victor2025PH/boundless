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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

_VALID_CLS = ("A", "B", "C")


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
        key="inbox.reply_style.bubbles.enabled", cls="A", slug="bubbles",
        baseline=True,
        note="多句分条回复（reply_split.parse_bubbles_cfg 总闸；真发仍逐条前端"
             "opt-in，默认开安全）；2026-07-31 拍板升 A"),
    # ── B 类：可解锁（依赖齐了可一键开；零依赖 B=未拍板进基线的纯软件功能） ──
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
        key="companion.proactive_topic.enabled", cls="C", slug="proactive",
        reason="risk", gate_feature="companion",
        note="AI 主动外呼=账号风险行为，开闸属运营决策，不随安装包默认开"),
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
        key="contacts.enabled", cls="C", slug="contacts", reason="pending",
        show=False,
        note="跨平台联系人/交接子系统未做桌面形态评估（种子头注释点名保持关）"),
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
        key="inbox.l2_autosend.deliver", cls="C", slug="autosend_deliver",
        reason="safety", show=False,
        note="AI 亲自发消息的总闸；新装默认必须人审（与 example 安全口径一致）"),
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
        if f.cls == "C" and not f.reason:
            raise ValueError(f"feature_registry: C 类必须给 reason 码（{f.key}）")
        for code in f.requires:
            if code not in DEP_CHECKERS:
                raise ValueError(
                    f"feature_registry: 未注册的依赖码 {code!r}（{f.key}）")


_validate()


def by_key(key: str) -> Optional[Feature]:
    for f in FEATURES:
        if f.key == key:
            return f
    return None


def product_baseline_map() -> Dict[str, str]:
    """A 类 {key: note}——种子门禁/文档用。"""
    return {f.key: f.note for f in FEATURES if f.cls == "A"}


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
            out[f.key] = f.baseline
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


def feature_state(f: Feature, cfg: dict) -> str:
    """确定性状态：on | available | needs_dep | locked。

    已开启（不论分级）如实报 on——运营机上 C 类经 overlay 打开是合法部署形态，
    总览必须与真实行为一致，不装「未开放」。
    """
    if bool(dig(cfg, f.key)):
        return "on"
    if f.cls == "C":
        return "locked"
    if missing_deps(f, cfg):
        return "needs_dep"
    return "available"


def ui_features() -> Tuple[Feature, ...]:
    return tuple(f for f in FEATURES if f.show)
