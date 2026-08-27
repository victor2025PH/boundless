"""系统级 AI 披露语（WP-4：EU AI Act Art.50 / SB 243 / NY §1700 的「披露」半边）。

语义：``compliance.disclosure.notice`` 开启时，**每个会话的首条 AI 出站**前置一段
披露语（按会话语言取内置多语模板，或运营方整条覆写），此后该会话永不重发——
防重标记持久落实例数据根（重启不重发，验收硬条款）。

设计要点：
- **文案基调**（规格视角三 C 线验收）：像客服团队公告，不像法务免责声明——
  「本对话由 AI 助理协助回复，人工团队随时可接入」，绝不写「免责/条款/概不负责」。
- **持久防重**不落 inbox ``conversation_meta``（规格原案）而用本模块自带 JSON
  标记库：A 线（telegram 协议链）与 B 线（inbox 草稿链）的会话键都能用同一张表，
  且零 store.py 迁移（该文件正被 msg-ops 线活跃编辑，schema 碰撞面为零）。
  键 = ``platform:account_id:chat_key``（调用方拼好传入）。
- **标记失败仍前置披露**：宁可偶发重复披露（合规无害）不可漏披露（合规风险）。
- 语言解析：显式 lang 提示优先；无提示按出站文本书写系统嗅探（CJK→zh、假名→ja、
  谚文→ko、泰文→th），拉丁系无法细分一律回落 en——回复本就按客户语言生成
  （既有语言守卫），嗅探面剩余误差可由运营方 ``text`` 覆写兜底。

接线现状（2026-08-17 全部回复线闭环）：
- **A 线 telegram 原生**：``src/client/sender.py::_send_reply``（rider ①，文本发送
  唯一收口；刻意不接 skill_manager 回复定稿处——语音分支在文本发送前消费回复
  文本，skill 层注入会让克隆声念出披露语）；
- **B 线 autosend**：``autosend_worker._apply_compliance_disclosure``（自动/人工
  投递两路共用）；
- **协议直发链**：``protocol_autoreply``（语言闸后、语音分支后）。
- **主动触达（rider ②，2026-08-17）**：deferred 家族（care/reactivation，5 平台）
  收口于 ``background_tasks._universal_send``（翻译后）；proactive_topic 四模式
  （topic/ritual/milestone/profile_ask）收口于开场发送闭包——披露命中**本轮强制
  文本**（跳过生活照/语音开场，首条主动接触必须是带披露的文本）。模板已场景
  中性化（zh/th/vi 去掉「回复」语境动词），单模板双场景通吃，免第二套模板。

接线样板统一走 ``apply_disclosure_for``（runtime 配置 + store 语言提示一站式，
拉丁语系客户靠提示出 es/pt/id/vi 披露语），门禁禁止绕过 helper 手拼样板。

统一口径：放在出站翻译/语言闸**之后**（披露语已是客户语言，再过翻译层=混语
garble 风险）、语音分支**之外**（克隆声绝不念披露语，语音先行的会话由首条文本
回复补披露）；标记只在真应用时烧；任何异常原样发送::

    from src.compliance.disclosure import apply_disclosure
    text, applied = apply_disclosure(config, f"{platform}:{acct}:{chat_key}", text,
                                     lang_hint=conv_lang)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.compliance import crisis_protocol_url, notice_enabled, notice_text_override

logger = logging.getLogger(__name__)

STATE_FILENAME = "compliance_disclosure.json"
_MAX_MARKS = 50000          # 防标记库无界膨胀（超限剔最旧；重复披露无害）

#: 内置披露语（对齐现有翻译语种面；客服公告基调，零法务腔）。
#: 客户面文案与既有惯例一致走代码内多语字典（selfie_stage_text / promise_fail
#: 同族）——web i18n pack 只服务后台 UI 语言（zh/en/vi/th/id），不覆盖客户语种面。
#: rider ②（2026-08-17）：文案**场景中性化**——同一条披露语既用于「回复」也用于
#: 「主动开场」（首触即 AI）。zh/th/vi 原稿带「回复/ตอบ/trả lời」动词，开场语境下
#: 不通；中性化后单模板双场景通吃，免去「9 语 × 变体」的第二套模板评审与维护。
#: 门禁 test_proactive_disclosure_wiring 钉住「不得再引入回复语境动词」。
_NOTICE_TEXTS: Dict[str, str] = {
    "zh": "您好～本对话由 AI 助理协助，人工团队随时可以接入。",
    "en": "Quick note: this chat is assisted by an AI assistant — a human team can step in at any time.",
    "es": "Nota rápida: este chat cuenta con un asistente de IA; un equipo humano puede intervenir en cualquier momento.",
    "pt": "Aviso rápido: este chat é assistido por um assistente de IA — uma equipe humana pode entrar a qualquer momento.",
    "id": "Info singkat: percakapan ini dibantu asisten AI — tim manusia dapat bergabung kapan saja.",
    "th": "แจ้งให้ทราบ: แชทนี้มีผู้ช่วย AI ให้บริการ และทีมงานที่เป็นมนุษย์พร้อมดูแลได้ตลอดเวลา",
    "vi": "Lưu ý nhỏ: cuộc trò chuyện này có trợ lý AI hỗ trợ — đội ngũ nhân viên có thể tham gia bất cứ lúc nào.",
    "ja": "ご案内：このチャットは AI アシスタントがお手伝いしています。必要に応じてスタッフがいつでも対応します。",
    "ko": "안내: 이 채팅은 AI 어시스턴트가 응대를 돕고 있으며, 필요 시 언제든 상담원이 참여할 수 있습니다.",
}

#: 危机协议 URL 追加行（运营方配置 crisis_protocol_url 时随披露语带出）
_URL_SUFFIX: Dict[str, str] = {
    "zh": "服务与安全说明：{url}",
    "en": "Service & safety info: {url}",
    "es": "Información de servicio y seguridad: {url}",
    "pt": "Informações de serviço e segurança: {url}",
    "id": "Info layanan & keamanan: {url}",
    "th": "ข้อมูลบริการและความปลอดภัย: {url}",
    "vi": "Thông tin dịch vụ & an toàn: {url}",
    "ja": "サービス・安全に関するご案内：{url}",
    "ko": "서비스 및 안전 안내: {url}",
}

_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_RE_KANA = re.compile(r"[\u3040-\u30ff]")
_RE_HANGUL = re.compile(r"[\uac00-\ud7af]")
_RE_THAI = re.compile(r"[\u0e00-\u0e7f]")

_LOCK = threading.Lock()
_CACHE: Optional[Dict[str, float]] = None          # {conversation_key: ts}


def pick_disclosure_lang(lang_hint: str = "", sample_text: str = "") -> str:
    """解析披露语语种：显式提示 → 文本书写系统嗅探 → en。恒返回支持集内的键。"""
    h = str(lang_hint or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if h in _NOTICE_TEXTS:
        return h
    s = str(sample_text or "")
    if _RE_KANA.search(s):
        return "ja"
    if _RE_HANGUL.search(s):
        return "ko"
    if _RE_THAI.search(s):
        return "th"
    if _RE_CJK.search(s):
        return "zh"
    return "en"


def disclosure_text(
    config: Optional[Dict[str, Any]], lang_hint: str = "", sample_text: str = ""
) -> str:
    """本次应发的披露语全文（运营覆写优先；URL 配了才带；绝不抛）。"""
    try:
        url = crisis_protocol_url(config)
        override = notice_text_override(config)
        if override:
            return override.replace("{url}", url).strip()
        lang = pick_disclosure_lang(lang_hint, sample_text)
        text = _NOTICE_TEXTS[lang]
        if url:
            text = text + "\n" + _URL_SUFFIX[lang].format(url=url)
        return text
    except Exception:
        return _NOTICE_TEXTS["en"]


# ── 持久防重标记 ─────────────────────────────────────────────────────────────

def _marks_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / STATE_FILENAME


def _load_cache() -> Dict[str, float]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    marks: Dict[str, float] = {}
    try:
        p = _marks_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("marks") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                marks = {str(k): float(v or 0) for k, v in raw.items()}
    except Exception:
        logger.debug("[compliance] 披露标记读取失败（按空表处理）", exc_info=True)
        marks = {}
    _CACHE = marks
    return _CACHE


def _save_cache() -> None:
    try:
        marks = _CACHE or {}
        if len(marks) > _MAX_MARKS:
            keep = sorted(marks.items(), key=lambda kv: kv[1])[-_MAX_MARKS:]
            marks = dict(keep)
            globals()["_CACHE"] = marks
        p = _marks_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"marks": marks}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[compliance] 披露标记落盘失败（下条可能重复披露，无合规风险）",
                       exc_info=True)


def already_disclosed(conversation_key: str) -> bool:
    key = str(conversation_key or "").strip()
    if not key:
        return False
    with _LOCK:
        return key in _load_cache()


def mark_disclosed(conversation_key: str) -> None:
    key = str(conversation_key or "").strip()
    if not key:
        return
    with _LOCK:
        _load_cache()[key] = time.time()
        _save_cache()


def marks_count() -> int:
    with _LOCK:
        return len(_load_cache())


def _reset_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


# ── 出站接线入口 ─────────────────────────────────────────────────────────────

def apply_disclosure(
    config: Optional[Dict[str, Any]],
    conversation_key: str,
    text: str,
    *,
    lang_hint: str = "",
) -> Tuple[str, bool]:
    """出站文本前置披露语（每会话一次）。返回 ``(text', applied)``，绝不抛。

    - 开关关 / 空文本 / 已披露过 → 原样返回 ``applied=False``；
    - 应披露 → ``披露语\\n\\n原文``，并持久标记（标记失败仍披露——宁重复不遗漏）。
    """
    try:
        t = str(text or "")
        if not t or not notice_enabled(config):
            return t, False
        key = str(conversation_key or "").strip()
        if not key or already_disclosed(key):
            return t, False
        notice = disclosure_text(config, lang_hint=lang_hint, sample_text=t)
        if not notice:
            return t, False
        mark_disclosed(key)
        return notice + "\n\n" + t, True
    except Exception:
        logger.debug("[compliance] apply_disclosure 异常（原样放行）", exc_info=True)
        return str(text or ""), False


def apply_disclosure_for(
    conversation_id: str,
    text: str,
    *,
    lang_hint: str = "",
    store: Any = None,
) -> Tuple[str, bool]:
    """接线样板一站式（rider ②，2026-08-17）：runtime 配置 + 会话语言提示 +
    ``apply_disclosure``，所有出站接线点（A 线 sender / deferred 投递 /
    proactive 开场）统一走本入口，防三处样板各自漂移。

    - 配置取 ``src.compliance.runtime.runtime_config()``（provider 未注册 → 空
      配置 → 恒 no-op）；开关关时**提前返回**，不碰 store（热路径零成本）；
    - ``lang_hint`` 缺省且给了 ``store`` → best-effort 取
      ``peer_language_hint``（拉丁语系客户嗅探不出细分语种、会回落 en，
      提示在场才能出 es/pt/id/vi 披露语）；
    - 任何异常返回 ``(原文, False)``，绝不抛、绝不阻断发送。
    """
    try:
        from src.compliance.runtime import runtime_config
        cfg = runtime_config()
        if not notice_enabled(cfg):
            return str(text or ""), False
        lh = str(lang_hint or "")
        if not lh and store is not None:
            try:
                from src.inbox.outbound_translate import peer_language_hint
                lh = peer_language_hint(store, str(conversation_id or "")) or ""
            except Exception:
                lh = ""
        return apply_disclosure(cfg, str(conversation_id or ""), text,
                                lang_hint=lh)
    except Exception:
        logger.debug("[compliance] apply_disclosure_for 异常（原样放行）",
                     exc_info=True)
        return str(text or ""), False


__all__ = [
    "STATE_FILENAME",
    "pick_disclosure_lang",
    "disclosure_text",
    "already_disclosed",
    "mark_disclosed",
    "marks_count",
    "apply_disclosure",
    "apply_disclosure_for",
]
