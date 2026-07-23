"""新会话「初始语言先验」——第一句该用什么语言写（lang_policy 的 default 供给源）。

治理场景（2026-07-23 「新 WA 好友第一句回日语」事故的文本侧收尾）：新好友第一条
消息常是 "Hi" / emoji / 贴图——语言中性，`classify_evidence` 判不出任何证据，
决策链一路落空到 `default="zh"`，给菲律宾/泰国客户回中文首句：体验差、且一开口
就暴露号源背景。音色侧已由 voice_lang_route.follow_text 治理，本模块补文本侧。

设计（先验，不是覆写）：
  - 只在语言决策链**全部落空**时生效——作为 `resolve_conversation_language` 的
    `default` 入参；任何真实语言证据 / 明确请求 / 历史窗口 / 粘滞语言都优先。
    客户一旦发出带证据的消息，先验即让位（跟随语义完全不变）。
  - 优先级：账号级配置（`platform:account` 精确键 > `platform` 平台键）
    ＞ 电话号码国码推断（仅 chat_key 是国际电话号码的平台，默认 whatsapp——
    Telegram 数字 user_id 形似电话号码，必须平台白名单防误判）＞ ""（维持旧
    行为，调用方自行回落 zh）。
  - 纯函数、零 IO、任何异常返回 ""；`lang_prior.enabled` 基线默认关（行为不变），
    实例 overlay 开。

配置（config.yaml::lang_prior）：
  lang_prior:
    enabled: false
    phone_platforms: [whatsapp]      # chat_key=E.164 号码的平台白名单
    account_defaults: {}             # {"whatsapp": "en", "whatsapp:63927...": "en"}
    country_overrides: {}            # {"63": "en"} 国码→语言 覆写/补充内置表

接线点（default 语义，均不改变有证据时的行为）：
  - skill_manager 3b（A 线 process_message + 协议线 protocol_autoreply）：
    `default=_prev_lang or hint or "zh"`；
  - persona_reply.generate_persona_reply（收件箱草稿/L2 autosend 产线）：
    `resolve_reply_language(..., default=hint or "zh")`；
  - drafts.py R1 auto_greeting：`detect_language(t) or hint or "zh"`。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from src.ai.lang_policy import normalize_lang_code

__all__ = [
    "PHONE_CC_LANG",
    "phone_lang_hint",
    "initial_lang_hint",
]


# ── 国际电话区号 → 语言（内置表，只出策略层已知语言码）────────────────
#
# 口径：该国**客服/商务场景**最常用书面语，不是官方语言全集——多语国家取
# 商务通用语（PH/IN/MY → en）。有争议/小众市场宁缺勿错（查不到返回 ""，
# 回落调用方默认），运营可用 country_overrides 精调（如 63 → fil）。
PHONE_CC_LANG: Dict[str, str] = {
    # 大中华
    "86": "zh", "852": "zh", "853": "zh", "886": "zh",
    # 东北亚
    "81": "ja", "82": "ko",
    # 东南亚（主要市场）
    "63": "en",   # 菲律宾：商务/客服英语通用（fil 可经 override 精调）
    "65": "en", "60": "en",  # 新加坡 / 马来西亚（商务英语）
    "66": "th", "84": "vi", "62": "id",
    # 南亚（多语，商务英语兜底）
    "91": "en", "92": "en", "880": "en",
    # 英语圈
    "1": "en", "44": "en", "61": "en", "64": "en", "27": "en",
    "234": "en", "233": "en", "254": "en", "353": "en",
    # 俄语圈
    "7": "ru", "375": "ru", "998": "ru", "996": "ru", "992": "ru",
    # 西欧
    "33": "fr", "32": "fr", "49": "de", "43": "de", "41": "de",
    "39": "it", "34": "es", "351": "pt",
    # 拉美
    "52": "es", "54": "es", "51": "es", "56": "es", "57": "es",
    "58": "es", "593": "es", "591": "es", "595": "es", "598": "es",
    "506": "es", "507": "es", "502": "es", "503": "es", "504": "es",
    "55": "pt",
    # 中东/北非（阿拉伯语）
    "20": "ar", "212": "ar", "213": "ar", "216": "ar", "218": "ar",
    "961": "ar", "962": "ar", "963": "ar", "964": "ar", "965": "ar",
    "966": "ar", "967": "ar", "968": "ar", "971": "ar", "973": "ar",
    "974": "ar", "249": "ar",
    # 土耳其
    "90": "tr",
}

# E.164：国码+号码共 7-15 位纯数字（可带 +）。WA 群 JID（120xxx…@g.us）
# 本体 18 位左右，天然被长度上限挡掉。
_PHONE_RE = re.compile(r"^\+?(\d{7,15})$")


def phone_lang_hint(
    chat_key: str, *, overrides: Optional[Dict[str, Any]] = None
) -> str:
    """从电话号码形 chat_key 推断语言先验；非号码/未知国码返回 ""。

    兼容 baileys JID 尾缀（``639…@s.whatsapp.net`` / ``639…:12@…`` 设备位）。
    国码按最长前缀匹配（3 → 2 → 1 位）。overrides 覆写/补充内置表
    （值经 normalize_lang_code 归一；显式配置不做已知码校验，信运营）。
    """
    s = str(chat_key or "").strip()
    if not s:
        return ""
    # 剥 JID 域名与设备位
    s = s.split("@", 1)[0].split(":", 1)[0].strip()
    m = _PHONE_RE.match(s)
    if not m:
        return ""
    digits = m.group(1)
    table = dict(PHONE_CC_LANG)
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            ks = str(k or "").strip().lstrip("+")
            vs = normalize_lang_code(str(v or ""))
            if ks.isdigit() and vs:
                table[ks] = vs
    for ln in (3, 2, 1):
        cc = digits[:ln]
        if cc in table:
            return table[cc]
    return ""


def initial_lang_hint(
    *,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """新会话首句语言先验（无任何语言证据时的 default 供给）。

    返回语言码或 ""（禁用/无先验——调用方维持既有默认）。绝不抛异常。
    """
    try:
        cfg = (config or {}).get("lang_prior") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return ""
        p = str(platform or "").strip().lower()
        acct = str(account_id or "").strip()

        # 1) 账号级显式配置：precision 键 platform:account > 平台键 platform
        defaults = cfg.get("account_defaults") or {}
        if isinstance(defaults, dict) and p:
            for key in ((f"{p}:{acct}" if acct else ""), p):
                if key and key in defaults:
                    v = normalize_lang_code(str(defaults.get(key) or ""))
                    if v:
                        return v

        # 2) 电话号码国码推断（平台白名单：chat_key 必须真的是电话号码）
        plats = cfg.get("phone_platforms")
        if plats is None:
            plats = ["whatsapp"]
        try:
            plat_set = {str(x).strip().lower() for x in plats if str(x).strip()}
        except TypeError:
            plat_set = {"whatsapp"}
        if p in plat_set:
            return phone_lang_hint(
                chat_key, overrides=cfg.get("country_overrides") or {})
        return ""
    except Exception:
        return ""
