"""
发送 Mixin：消息发送、回复分段、术语替换、日志脱敏
"""

import asyncio
import html
import os
import random
import re
import time
from typing import Any, Dict, List, Optional

from src.client import daily_stats

# A1 text-first 占位句内置池（config reply.ai_fallback_replies 缺席时的兜底备货——
# 2026-07-26 实锤：实例配置池为空 → 硬编码单句「稍等我一下哈～」4 分钟连发 3 次，
# 触发出站复读告警，真人不会这样说话）。
# ⚠ 2026-08-16 起占位句默认关闭（text_first.filler 默认 False，老板裁定「宁可
# 不发消息也不发垫场句」）；本池仅在运营显式开 filler: true 且配置池为空时使用。
_TF_FILLER_DEFAULTS = (
    "稍等我一下哈～",
    "等我一下下哈～",
    "来了来了，稍微等我一下～",
    "嗯嗯在的，马上回你～",
    "手头有点小事，马上就来～",
)


def pick_text_first_filler(
    pool, *, last_text: str = "", last_ts: float = 0.0,
    now: Optional[float] = None, cooldown_sec: float = 180.0,
) -> Optional[str]:
    """text-first 占位句选择（纯函数）。

    - 冷却窗内（该会话 ``cooldown_sec`` 秒内已发过占位）→ ``None``＝本轮不发：
      对方刚被「稍等」过一次，紧接着又「稍等」只会像机器人——语音/兜底文字反正会到；
    - 出窗 → 从 ``pool``（空则内置池）随机挑一句，并避开该会话上一条占位措辞。
    """
    now_ts = time.time() if now is None else float(now)
    try:
        cd = float(cooldown_sec or 0)
    except (TypeError, ValueError):
        cd = 0.0
    if cd > 0 and float(last_ts or 0) > 0 and (now_ts - float(last_ts)) < cd:
        return None
    cand = [str(x).strip() for x in (pool or []) if str(x).strip()]
    if not cand:
        cand = list(_TF_FILLER_DEFAULTS)
    fresh = [x for x in cand if x != str(last_text or "")]
    return random.choice(fresh or cand)


class TelegramSenderMixin:

    def _reply_to_message_id_for_send(self, original_message) -> Optional[int]:
        """Telegram reply / quote bar: off for natural chat when configured or conversion domain."""
        tg = (self.config.get("telegram") or {}) if getattr(self, "config", None) else {}
        # UI「回复逻辑」页写的是 telegram.reply_logic.reply_to_user_message → 优先；
        # 顶层同名键仅作旧配置回退。
        rl = tg.get("reply_logic") or {}
        if isinstance(rl, dict) and "reply_to_user_message" in rl:
            return int(original_message.id) if rl.get("reply_to_user_message") else None
        if "reply_to_user_message" in tg:
            return int(original_message.id) if tg.get("reply_to_user_message") else None
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(raw, dict) and effective_domain_name(raw) == "conversion":
                return None
        except Exception:
            pass
        return int(original_message.id)

    def _sanitize_parenthetical_stage_directions(self, text: str) -> str:
        """Strip short （…）/(...) asides typical of LLM stage directions; conversion domain only."""
        if not text:
            return text
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(raw, dict) or effective_domain_name(raw) != "conversion":
                return text
        except Exception:
            return text
        t = text
        t = re.sub(r"（[^）]{1,28}）", "", t)
        t = re.sub(r"\([^)]{1,32}\)", "", t)
        return re.sub(r"[ \t\f\v]{2,}", " ", t).strip()

    def _rewrite_companion_helpdesk_ping(
        self, reply: str, user_message: str
    ) -> str:
        """conversion 域：用户短寒暄/探询（在吗等）时，避免「有什么可以帮」类客服套话。"""
        if not reply or not (user_message or "").strip():
            return reply
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(raw, dict) or effective_domain_name(raw) != "conversion":
                return reply
        except Exception:
            return reply
        try:
            from src.utils.greeting_lexicon import (
                is_greeting_message,
                is_standalone_zai_query,
            )
        except Exception:
            return reply
        u = (user_message or "").strip()
        if len(u) > 36:
            return reply
        if not (is_greeting_message(u) or is_standalone_zai_query(u)):
            return reply
        markers = (
            "有什么可以帮",
            "请问有什么",
            "需要什么服务",
            "竭诚为您",
            "为您服务",
        )
        if not any(m in reply for m in markers):
            return reply
        if len(reply) <= 80:
            return random.choice(
                (
                    "嗯嗯我在～怎么啦？",
                    "在呀，找我呢？",
                    "在的，你说～",
                    "来啦～刚还在看手机",
                )
            )
        for old, new in (
            ("在的，有什么可以帮您的？", "在呀～"),
            ("在的，有什么可以帮您？", "在呀～"),
            ("有什么可以帮您的？", "怎么啦？"),
            ("有什么可以帮您？", "怎么啦？"),
        ):
            if old in reply:
                reply = reply.replace(old, new, 1)
        return reply

    def _apply_terminology(self, text: str) -> str:
        if not (text and isinstance(text, str)):
            return text or ""
        terms = (self.config.get("ai") or {}).get("terminology") or {}
        if not isinstance(terms, dict):
            return text
        for wrong, right in sorted(terms.items(), key=lambda x: -len(x[0])):
            if wrong and right is not None:
                text = text.replace(wrong, str(right))
        return text

    def _split_at_safe_boundary(self, text: str, max_pos: int) -> int:
        if max_pos >= len(text):
            return len(text)
        pay_in = re.search(r"Pay\s+in", text, re.I)
        if pay_in:
            a, b = pay_in.start(), pay_in.end()
            if a < max_pos < b:
                return a if max_pos - a <= b - max_pos else b
        pay_out = re.search(r"Pay\s+out", text, re.I)
        if pay_out:
            a, b = pay_out.start(), pay_out.end()
            if a < max_pos < b:
                return a if max_pos - a <= b - max_pos else b
        for m in re.finditer(r"\bEP\b|\bJC\b", text):
            a, b = m.start(), m.end()
            if a < max_pos < b:
                return a if max_pos - a <= 1 else b
        for m in re.finditer(r"\d{4,}", text):
            a, b = m.start(), m.end()
            if a < max_pos < b:
                return a if max_pos - a < b - max_pos else b
        slice_ = text[:max_pos]
        for sep in ("\n", "。", "！", "？", ".", "!", "?", "，", ",", ";", "；"):
            idx = slice_.rfind(sep)
            if idx >= max_pos // 2:
                return idx + 1
        idx = slice_.rfind(" ")
        if idx >= max_pos // 2:
            return idx + 1
        return max_pos

    def _chunk_segment_safe(self, seg: str, max_chars: int) -> List[str]:
        seg = seg.strip()
        if not seg:
            return []
        if len(seg) <= max_chars:
            return [seg]
        out: List[str] = []
        rest = seg
        while len(rest) > max_chars:
            cut = self._split_at_safe_boundary(rest, max_chars)
            if cut <= 0:
                cut = max_chars
            piece = rest[:cut].strip()
            if piece:
                out.append(piece)
            rest = rest[cut:].strip()
        if rest:
            out.append(rest)
        return out if out else [seg]

    def _split_reply_for_send(
        self,
        text: str,
        max_chars_per_message: int = 120,
        min_segments_to_split: int = 2,
    ) -> List[str]:
        s = (text or "").strip()
        if not s:
            return []
        if len(s) <= max_chars_per_message:
            return [s]
        segments = [t.strip() for t in re.split(r"\n\s*\n", s) if t.strip()]
        if len(segments) < min_segments_to_split:
            return self._chunk_segment_safe(s, max_chars_per_message)
        chunks: List[str] = []
        for seg in segments:
            if len(seg) <= max_chars_per_message:
                chunks.append(seg)
            else:
                sentences = re.split(r"(?<=[。！？.!?])\s*", seg)
                sentences = [x.strip() for x in sentences if x.strip()]
                current = ""
                for sent in sentences:
                    if len(sent) > max_chars_per_message:
                        if current:
                            chunks.append(current)
                            current = ""
                        chunks.extend(self._chunk_segment_safe(sent, max_chars_per_message))
                        continue
                    if not current:
                        current = sent
                    elif len(current) + len(sent) + 1 <= max_chars_per_message:
                        current = (current + " " + sent) if current else sent
                    else:
                        if current:
                            chunks.append(current)
                        current = sent
                if current:
                    chunks.append(current)
        return chunks if chunks else [s]

    def _log_safe_text(self, text: str, max_chars: Optional[int] = None) -> str:
        log_cfg = (self.config.get("logging") or {}).get("desensitize") or {}
        if not log_cfg.get("enabled", False):
            return (text or "")[: max_chars or 500]
        max_c = int(log_cfg.get("max_chars", 80) or 80)
        max_digit = int(log_cfg.get("max_digit_run", 6) or 6)
        s = text or ""
        if max_digit > 0:
            s = re.sub(r"\d{%d,}" % max_digit, "***", s)
        if len(s) > max_c:
            s = s[:max_c] + "…"
        return s

    def _shared_send_limiter(self, cfg):
        """取与 B 线协议自动回复共用的 AutoReplyLimiter 单例（一个计数器喂两线）。

        失败返回 None（闸门/计数静默降级，绝不阻断 A 线发送）。
        """
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            return get_autoreply_limiter(cfg or {})
        except Exception:
            return None

    # ── 统一发送护栏/节流/记账（A 线文本回复 + 形象照直发共用一套，防图文混发绕过风控） ──

    def _presend_blocked(self, *, is_autoreply: bool = False,
                         peer: Any = None) -> bool:
        """发送前统一护栏：G1 全局 Kill-Switch + N 线反封号闸门 + 账号限速/熔断 + 营业时段。

        返回 True=应跳过本次外发（冻结/被闸门拦）；任何异常一律静默放行（绝不因护栏自身报错阻断发送）。
        文本回复与形象照直发共用本判断——避免「文字被拦但图照发」的风控绕过。

        ``is_autoreply``：True=本次是「入站自动回复」（受营业时段 hours 约束——非营业时段
        转人工不自动发）；False=主动发/坐席接管/编排器/测试（只受限速与急停，不被时段拦，
        坐席深夜也能联系客户）。限速（时/日上限+熔断）对两类外发一律生效（账号级安全）。

        ``peer``（#77，0830 AW7MUV 实锤）：本次发送的目标 chat id（可选）。两用：
        ① 反封号闸门接 ``exempt_peers`` 白名单豁免——此前 A 线**完全没接**白名单
        （B 线/编排器早有），白名单客户的 A 线自动回复照样被 daily_cap 拦；
        ② 拦截日志带目标 peer（「拦的是白名单客户还是别人」从此可定性）。
        不传＝行为与旧版一致（无豁免、日志无 peer）。
        """
        # License 到期硬阻断（Sprint2）：enforce 开且授权失效(只读) → 跳过 A 线外发。
        # 默认 enforce=false → 恒放行，零破坏；fail-open。
        try:
            from src.licensing.gate import is_outbound_blocked
            from src.licensing.license_manager import get_license_manager
            if is_outbound_blocked(get_license_manager().status()):
                self.logger.warning("[license] 授权失效只读，跳过 A 线外发")
                return True
        except Exception:
            pass
        try:
            from src.ops.kill_switch import is_blocked as _ks_blocked
            _ks_on, _ks_scope, _ = _ks_blocked(
                "telegram", getattr(self, "account_id", "default"))
            if _ks_on:
                self.logger.warning(
                    "[kill-switch] 冻结发送，跳过 A 线外发（scope=%s）", _ks_scope)
                return True
        except Exception:
            pass
        # 防御式取配置：mixin 消费方可能没有 config 属性（如轻量测试替身/局部装配的
        # sender）——曾因此处裸访问 self.config 抛 AttributeError，让"护栏自身报错
        # 不阻断发送"的承诺失效（send_photo 全量失败）。
        _cfgobj = getattr(self, "config", None)
        if hasattr(_cfgobj, "config"):
            _gcfg = _cfgobj.config or {}
        elif isinstance(_cfgobj, dict):
            _gcfg = _cfgobj
        else:
            _gcfg = {}
        _acct = getattr(self, "account_id", "default")
        # ── 营业时段（仅约束自动回复）：非营业时段不自动发，转人工由坐席上班处理 ──
        if is_autoreply:
            try:
                from src.integrations.protocol_autoreply import within_business_hours
                if not within_business_hours(_gcfg):
                    self.logger.info("[hours] 账号 %s 非营业时段，自动回复转人工（不自动发）", _acct)
                    return True
            except Exception:
                pass
        # ── 账号级限速 + 熔断（时/日上限；A/B 两线共用同一 limiter 计数，防封号）──
        try:
            from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
            _pa = (_gcfg.get("protocol_autoreply") or {})
            _rate = (_pa.get("rate") or {})
            # 仅当显式配了 rate（hourly/daily 任一 >0）才启用限速，未配=零破坏不拦
            if int(_rate.get("hourly", 0) or 0) > 0 or int(_rate.get("daily", 0) or 0) > 0:
                _lim = get_autoreply_limiter(_gcfg)
                if _lim is not None:
                    _ok, _why = _lim.allow(f"telegram:{_acct}")
                    if not _ok:
                        self.logger.warning("[rate] 账号 %s 限速/熔断拦截自动外发: %s", _acct, _why)
                        return True
        except Exception:
            pass
        # ── 反封号健康闸门（预热 cap + 红黄绿灯）──
        try:
            from src.skills.companion_send_gate import (
                evaluate, gate_enabled, peer_exempt,
            )
            from src.skills.account_signals import build_account_signals
            if gate_enabled(_gcfg):
                # #77：exempt_peers 白名单豁免——A 线此前没接（B 线/编排器早有），
                # 白名单客户的 A 线自动回复照样被额度拦。命中即放行 + INFO 留痕。
                if peer is not None and peer_exempt(_gcfg, str(peer)):
                    self.logger.info(
                        "[send_gate] 白名单豁免命中 telegram:%s → peer=%s"
                        "（本次发送不受额度限制）", _acct, peer)
                    return False
                # N3 修：A 线此前只传 limiter，缺 registry → age_days/banned/status 恒缺省，
                # 使「号被封禁/移除」无法自动停发（反封号闸门形同虚设）。补传 registry，
                # 让 banned=meta.banned or status==removed 真正生效（best-effort，取不到不阻断）。
                _reg = None
                try:
                    from src.integrations.account_registry import get_account_registry
                    _reg = get_account_registry()
                except Exception:
                    _reg = None
                _sig = build_account_signals(
                    "telegram", _acct,
                    registry=_reg,
                    limiter=self._shared_send_limiter(_gcfg),
                    extra={"proxy_bound": bool(getattr(self, "proxy_id", ""))},
                )
                _dec = evaluate(_sig, _gcfg)
                if not _dec.get("allowed", True):
                    # #77：日志带目标 peer——「拦的是谁」从此可定性
                    self.logger.warning(
                        "[send_gate] 账号 %s 被反封号闸门拦截 → peer=%s: %s "
                        "(light=%s, score=%s)",
                        _sig["account_id"], peer if peer is not None else "?",
                        _dec.get("reason"), _dec.get("light"), _dec.get("score"),
                    )
                    return True
        except Exception:
            pass
        return False

    async def _presend_pace(self) -> None:
        """发送间隔节流：距上次外发不足 ``reply.split_send.min_interval_seconds`` 则补足。

        文本与照片共用同一 ``_last_send_wallclock`` 基准——图文混发也排队、不会瞬时双发触发反垃圾。
        异常静默（节流自身出错不阻断发送）。
        """
        try:
            split_cfg = self.config.get("reply", {}).get("split_send", {})
            min_interval = float(split_cfg.get("min_interval_seconds", 0) or 0)
            last = float(getattr(self, "_last_send_wallclock", 0) or 0)
            if min_interval > 0 and last > 0:
                elapsed = time.time() - last
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
        except Exception:
            pass

    async def _mark_peer_read(self, chat_id) -> None:
        """回复前对该会话补平台「已读」回执（拟人：真人先看后回）。

        不补的话对端客户端上会出现「消息还是未读却收到了回复」的机器人破绽。
        best-effort：任何异常只记 debug，绝不阻断发送主流程。
        """
        if chat_id is None:
            return
        try:
            if self.client is not None and hasattr(self.client, "read_chat_history"):
                await self.client.read_chat_history(chat_id)
        except Exception:
            self.logger.debug("[mark_read] 已读回执失败 chat=%s", chat_id, exc_info=True)

    async def _send_typing_action(self, chat_id) -> None:
        """挂 Telegram「正在输入」状态（best-effort，约 5s 自动过期）。

        与 ``_voice_recording_action``（正在录音）对称——文本回复的拟人打字气泡。
        """
        if chat_id is None:
            return
        try:
            from pyrogram.enums import ChatAction
            await self.client.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass

    async def _send_upload_photo_action(self, chat_id) -> None:
        """挂「正在发送照片」状态（best-effort，约 5s 自动过期）。

        媒体版打字气泡：相册/自拍出站前的拟人挑图等待期间客户看得到动静
        （skill_manager ``_media_presend_pacing`` 经 ``_send_media_action`` 消费）。
        """
        if chat_id is None:
            return
        try:
            from pyrogram.enums import ChatAction
            await self.client.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        except Exception:
            pass

    async def run_prereply_humanize(
        self, chat_id, *, text: str = "", elapsed_sec: float = 0.0,
    ) -> None:
        """原生 A 线文本回复前的拟人序列：已读 → 静默思考 → 正在输入(续挂) → 发送。

        与全自动 autosend 共用 ``humanize`` 协作器与 ``resolve_pacing`` 装配，节奏一致。
        延迟配置解析（2026-08-04 单一节奏源收口）：
          1. ``telegram.reply_humanize.thinking_delay`` **实际配了值**（max_sec>0）→ 用它
             （A 线独立覆写，运营显式选择时优先）；
          2. 未配置/为零（出厂基准就是显式 0/0 块，**不能**按「块存在」判定——否则
             回落在所有标准部署上永远死路）→ **跟随** ``inbox.l2_autosend.deliver_delay``
             （设置页「回复节奏」滑杆写的就是这个键——修「设置页调了节奏、A 线原生
             回复不吃」的双源分裂；``platform_overrides.telegram`` /
             ``persona_overrides`` 同步生效）；
          3. 想要「B 线有延迟、A 线保持秒回」的旧行为 → ``thinking_delay.follow: false``
             显式退出跟随。
        两处都没配 → 0=不延迟（只已读，保持旧手感）。
        ``adaptive=false`` 走 uniform(min,max)；``adaptive=true`` 按回复 ``text`` 长度/
        激活度自适应估时并扣除 ``elapsed_sec`` 已耗时（入站至今）。
        打字气泡走**两段式**（typing_lead）：思考期静默、临发前才挂「正在输入」——
        真人不会为 20 个字打 45 秒字。语音回复不走此路（自带「正在录音」分条节奏）。
        best-effort。
        """
        if chat_id is None:
            return
        try:
            raw_cfg = self.config.config if hasattr(self.config, "config") else {}
            rh = (raw_cfg.get("telegram") or {}).get("reply_humanize") or {}
            from src.inbox.humanize import (
                resolve_following_delay_block,
                resolve_pacing,
                resolve_typing_lead,
                run_presend_humanization,
            )
            # 单一节奏源（2026-08-07）：thinking_delay 未独立配值时跟随设置页滑杆键
            # deliver_delay。follow 判定收口于 resolve_following_delay_block，
            # 与协议链 / coverage 自检同一函数（改 follow 规则不再三处漂移）。
            _block, _ = resolve_following_delay_block(
                rh.get("thinking_delay"),
                (((raw_cfg.get("inbox") or {}).get("l2_autosend") or {})
                 .get("deliver_delay")))
            # 人设化节奏 + 观测分维：账号人设（与语音路径同口径 account_persona_ids[0]）。
            _pid = ""
            try:
                _pids = getattr(self, "account_persona_ids", None)
                _pid = str(_pids[0]) if _pids else ""
            except Exception:
                _pid = ""
            # arousal 由 resolve_pacing 从回复 text 自动估（语义正确：回复自身激活度）。
            _pr = resolve_pacing(
                _block, text=text, elapsed_sec=elapsed_sec,
                persona_id=_pid, platform="telegram")
            delay = _pr.delay
            # P2b 连发间隔地板（2026-08-12，A 线实测 4.1s 双发后补齐）：与 B 线
            # worker / 协议链同一对纯函数。A 线的特殊性＝interject 撕稿重来会造出
            # **多个并发在途回复**（各睡各的延迟先后落地）——预先算地板时对方还没
            # 发出、账本是空的，所以除了睡前预检，睡醒后还要**再对账一次**（见下）。
            _gap = 0.0
            _floored = False
            _ck = str(chat_id)
            try:
                from src.inbox.humanize import (
                    apply_min_gap_floor,
                    resolve_min_gap_sec,
                )
                _gap = resolve_min_gap_sec(
                    _block, platform="telegram", persona_id=_pid)
            except Exception:
                _gap = 0.0
            _ledger = getattr(self, "_conv_last_sent", None)
            _prev_pre = _ledger.get(_ck) if _ledger else None
            if _gap > 0 and _prev_pre is not None:
                delay, _floored = apply_min_gap_floor(
                    delay,
                    since_last_send_sec=max(0.0, time.time() - _prev_pre),
                    min_gap_sec=_gap)

            async def _mr():
                await self._mark_peer_read(chat_id)

            async def _tp(_action):
                await self._send_typing_action(chat_id)

            await run_presend_humanization(
                delay=delay, action="typing",
                mark_read=_mr, typing=_tp, sleep=asyncio.sleep,
                typing_lead_sec=resolve_typing_lead(
                    _block, text=text, persona_id=_pid, platform="telegram"))
            # 睡醒后对账（并发窗兜底）：**只在账本于本次 sleep 期间被刷新**（同会话
            # 另一条在途回复真的落地了）时补垫——账本没变就不重掷抖动（否则同一次
            # 间隔会因两次随机数不同被小额双垫）。这是 02:40 实测 gap=4.1s 双发的
            # 直接解：两条回复都在 humanize sleep 里，谁先醒谁发，后醒的必须看见
            # 先发的那条。垫付期边垫边续挂「正在输入」（气泡 ~5s 过期，4s 一续），
            # 封顶一个完整间隔。
            if _gap > 0:
                _ledger = getattr(self, "_conv_last_sent", None)
                _prev = _ledger.get(_ck) if _ledger else None
                if _prev is not None and _prev != _prev_pre:
                    _pad, _f2 = apply_min_gap_floor(
                        0.0,
                        since_last_send_sec=max(0.0, time.time() - _prev),
                        min_gap_sec=_gap)
                    if _f2 and _pad > 0:
                        _floored = True
                        _pad = min(_pad, _gap)
                        while _pad > 0:
                            await self._send_typing_action(chat_id)
                            _step = min(4.0, _pad)
                            await asyncio.sleep(_step)
                            _pad -= _step
            if _floored:
                from dataclasses import replace as _dc_replace
                _pr = _dc_replace(_pr, floored=True)
            try:
                from src.integrations.humanize_metrics import record_pacing
                record_pacing(f"native_tg/{_pid or '-'}", _pr)
            except Exception:
                pass
        except Exception:
            self.logger.debug("[prereply_humanize] 失败（忽略）", exc_info=True)

    def _handle_send_exc(self, exc: Any) -> None:
        """A 线发送异常统一处置（三处发送路径共用）：G2 封号信号分级急停 + 实施31 TG 告警。

        风控错误 → ban_signal 分级（退避/暂停/封禁）：pause/ban 写账号级 Kill-Switch，
        ban 另标注册表 meta.banned（喂健康闸门→后续自动停发），并经 ops_alert 推 TG 告警。
        全程 best-effort，绝不抛（处置/告警失败不得掩盖原始发送错误）。
        """
        try:
            from src.ops.ban_signal import handle_send_exception as _g2
            from src.ops.ops_alert import make_ban_signal_alert
            _reg = None
            try:
                from src.integrations.account_registry import get_account_registry
                _reg = get_account_registry()
            except Exception:
                _reg = None
            _g2("telegram", getattr(self, "account_id", "default"), exc,
                registry=_reg, alert=make_ban_signal_alert())
        except Exception:
            pass

    def _postsend_record_count(self) -> None:
        """发送成功后统一记账：刷新墙钟 + 记入与 B 线共用的发送计数器。

        墙钟供下次 ``_presend_pace`` 节流；计数器喂反封号闸门 + 机群健康灯今日外发量（best-effort）。
        另喂渠道页「AI 已发」当日计数（daily_stats.replies，P2-0 四页 KPI 对齐）：
        口径=A 线自动链成功出站条数（文字/语音分条各计一条，与出站镜像同频）。
        """
        self._last_send_wallclock = time.time()
        try:
            daily_stats.bump("replies")
        except Exception:
            pass
        try:
            _lim = self._shared_send_limiter(
                self.config.config if hasattr(self.config, "config") else {}
            )
            if _lim is not None:
                _lim.record_sent(
                    f"telegram:{getattr(self, 'account_id', 'default')}")
        except Exception:
            pass

    def _postsend_mirror_and_record(self, chat_id: Any, preview: str,
                                    msg_id: Any = "",
                                    media_type: str = "",
                                    media_ref: str = "",
                                    mirror_text: Optional[str] = None) -> None:
        """发送成功后：出站镜像到坐席台（N4b）+ 记入 contacts 的外发互动（Q3）。

        文本回复与富媒体（照片/语音）共用。两步各自 best-effort，绝不阻断发送；
        实现拆在 ``_mirror_out_row`` / ``_record_contact_out``（2026-08-02，分条语音
        逐条镜像需要两个口径各自演进：收件箱 N 行、contacts 一轮一次）。

        ``msg_id``：发送 API 返回的真实 message.id（治本幂等键）。带上它后乐观出站镜像行与
        「自身已发消息被回显」共用同一 platform_msg_id → 主键级精确去重，不再依赖时间窗近似。

        ``media_type`` / ``media_ref``：富媒体镜像成**媒体行**而不只是文字占位
        （2026-07-31 补）。此前 A 线只写 ``[图片] 配文`` 这种纯文本，后果两条：坐席在
        工作台看不到自家人设发出去的图；按 ``media_type`` 统计出站媒体的口径把 A 线
        整条漏掉（实测 Telegram 868 条出站只数出 1 条，实际文本占位有 166 条）。

        ``mirror_text``：收件箱镜像行的正文覆写（None=沿用 preview）。媒体行的
        ``[语音]``/``[图片]`` 语义已由 media_type 承载，镜像正文应是干净的念稿/配文
        （与 B 线 ``send_media(inbox_text=…)`` 同口径）；而 contacts 时间线是纯文本，
        preview 保留标记才有表意。
        """
        # P2b 连发地板账本（2026-08-12）：每会话最近一次**成功发出**时刻。只在
        # 真发成功后落点（interject 撕稿/发送失败不占位），供 run_prereply_humanize
        # 的睡前预检 + 睡醒对账两处消费。软上限防长跑撑爆。
        try:
            _led = getattr(self, "_conv_last_sent", None)
            if _led is None:
                _led = {}
                self._conv_last_sent = _led
            _led[str(chat_id)] = time.time()
            if len(_led) > 4096:
                for _k in sorted(_led, key=_led.get)[:2048]:
                    _led.pop(_k, None)
        except Exception:
            pass
        self._mirror_out_row(
            chat_id, preview if mirror_text is None else mirror_text,
            msg_id=msg_id, media_type=media_type, media_ref=media_ref)
        self._record_contact_out(chat_id, preview)

    def _mirror_out_row(self, chat_id: Any, text: str, *, msg_id: Any = "",
                        media_type: str = "", media_ref: str = "",
                        sender_name: str = "") -> None:
        """出站镜像**单行**进收件箱（含「已发送」回执），不记 contacts；best-effort。

        分条语音（2026-08-02）逐条调用：客户收到 N 条独立语音，收件箱就该有 N 行
        （每行自己的 msg_id/念稿/media_ref，坐席可逐条回放）。

        ``sender_name``（P1-3）：语音行带「谁的音色」（人设显示名）——非空才透传，
        经 ``_emit_inbox`` 的 source 落 ``messages.sender_name``，坐席气泡显示徽标。
        """
        try:
            _emit = getattr(self, "_emit_inbox", None)
            if _emit is not None:
                _kw = {"sender_name": str(sender_name)} if sender_name else {}
                _emit(chat_id=chat_id, text=text, direction="out",
                      msg_id=str(msg_id or ""),
                      media_type=media_type or "", media_ref=media_ref or "",
                      **_kw)
                # P4-4：镜像出站即置「已发送」（单勾）；对端读后由 UpdateReadHistoryOutbox
                # 回执升级为「已读」（蓝色双勾）。仅 companion 镜像开启且带真实 id 时生效。
                if getattr(self, "_mirror_inbox", False) and msg_id:
                    from src.integrations.protocol_bridge import report_message_status
                    report_message_status(
                        "telegram", getattr(self, "account_id", "default"),
                        str(chat_id), str(msg_id), "sent")
        except Exception:
            pass

    def _record_contact_out(self, chat_id: Any, preview: str) -> None:
        """contacts 外发互动记账（IntimacyEngine mutuality 口径）；best-effort。

        与镜像行拆开：分条语音 N 行镜像仍只记**一轮**互动，保持既有亲密度动力学
        （真人一口气连发 3 条短语音也是一轮对话，不是三倍热情）。
        """
        try:
            from src.utils.companion_context import (
                record_relationship_message as _rec_rel_msg,
            )
            _rec_rel_msg(
                getattr(self, "account_id", "default"),
                chat_id, "out", text_preview=preview or "",
            )
        except Exception:
            pass

    @staticmethod
    def _voice_mirror_preview(spoken: str, parts_sent: int = 1) -> str:
        """语音出站镜像的文案：``[语音] 念稿``（多条则 ``[语音]×N 念稿``）。

        与 ``[图片] 配文`` 同构，也与 B 线 ``send_media(inbox_text=念稿)`` 同口径。
        念稿为空时退回裸标记（就是改动前的样子）。客户收到的仍是纯语音，这里只影响
        坐席台可读性与「AI 上次说了什么」的历史可见性。
        """
        tag = "[语音]" if parts_sent <= 1 else "[语音]×%d" % parts_sent
        text = " ".join(str(spoken or "").split())
        return f"{tag} {text}".strip() if text else tag

    def _publish_media_ref(self, local_path: str) -> tuple:
        """本地媒体文件 → ``(media_type, /static URL)``，供出站镜像成媒体行。

        best-effort：任何失败返回 ``("", "")``，调用方退回纯文本镜像（＝旧行为）。
        ⚠ 语音路径必须在 ``os.unlink`` **之前**调它——音频发完即删。
        """
        try:
            from src.integrations.protocol_bridge import publish_outbound_media
            url, mt = publish_outbound_media(
                "telegram", getattr(self, "account_id", "default"), local_path)
            return (mt, url) if url else ("", "")
        except Exception:
            return "", ""

    def _persona_display_name(self) -> str:
        """本账号人设显示名（B2 自称改写用）；拿不到 → 空串=过检自动跳过。"""
        cached = getattr(self, "_persona_name_cache", None)
        if cached is not None:
            return cached
        name = ""
        try:
            pids = getattr(self, "account_persona_ids", None) or []
            if pids:
                from src.utils.persona_manager import PersonaManager
                p = PersonaManager.get_instance().get_persona_by_id(str(pids[0]))
                if isinstance(p, dict):
                    name = str(p.get("name") or "").strip()
        except Exception:
            name = ""
        self._persona_name_cache = name
        return name

    def _record_auto_reply(self, chat_id, user_id) -> None:
        """回复逻辑记账：一次自动回复成功送出 → 刷新该用户的冷却时钟 + 连续计数。

        连续计数的过期复位语义与 ``reply_logic_gates`` 闸门共用 ``effective_streak``
        （距上次自动回复超 30 分钟 → 从 1 重新计，否则 +1），两处永远一致。
        键必须经 ``reply_logic_key``（账号分桶）——闸门读的就是这个键；此前这里
        写旧格式 ``{chat}:{user}``，读写不相交 → 冷却/连发上限静默失效（2026-08-03
        SpamBot 空转事故复盘时发现，修复回归钉在 test_reply_logic_gates）。
        best-effort：记账失败绝不影响发送主流程。
        """
        try:
            from src.client.reply_logic_gates import (
                effective_streak,
                reply_logic_key,
            )
            ts_map = getattr(self, '_auto_reply_ts', None)
            streak_map = getattr(self, '_auto_reply_streak', None)
            if ts_map is None or streak_map is None:
                return
            key = reply_logic_key(
                getattr(self, 'account_id', 'default'), chat_id, user_id)
            now = time.time()
            streak_map[key] = effective_streak(
                streak_map.get(key, 0), ts_map.get(key), now) + 1
            ts_map[key] = now
            # 防膨胀：超 5000 条时清理超过一天未互动的项（一天远超冷却/复位窗口，
            # 清掉语义安全；风格参照 _record_session_reply / _reject_cooldowns）
            if len(ts_map) > 5000:
                cutoff = now - 86400.0
                for k in [k for k, v in ts_map.items() if v < cutoff]:
                    ts_map.pop(k, None)
                    streak_map.pop(k, None)
        except Exception:
            self.logger.debug("[回复逻辑] 记账失败（忽略）", exc_info=True)

    async def _send_reply(self, original_message, reply_text: str, parse_mode=None):
        try:
            # 统一发送前护栏（与 send_photo 共用）：G1 Kill-Switch + 反封号闸门 + 限速 + 营业时段。
            # 这是「入站自动回复」路径 → is_autoreply=True（受营业时段约束；主动发/编排器不受时段拦）。
            if self._presend_blocked(
                    is_autoreply=True,
                    peer=getattr(getattr(original_message, "chat", None),
                                 "id", None)):
                return
            # 拟人已读回执：回复前先「看」消息（对端由未读变已读），再节流/发送。
            await self._mark_peer_read(
                getattr(getattr(original_message, "chat", None), "id", None))
            # 统一发送间隔节流（与 send_photo 共用同一墙钟，图文混发不瞬时双发）。
            await self._presend_pace()
            if not self.client:
                self.logger.error("客户端未初始化，无法发送回复")
                return
            _out_text = self._sanitize_parenthetical_stage_directions(reply_text)
            # B2 出站统一质量管道（2026-07-15）：无论文本来自 LLM/模板/占位/兜底，
            # 发送口统一过检——第三人称自称改写为「我」+ 同会话复读检测（指标）。
            try:
                from src.ai.outbound_quality import outbound_quality_pass
                _out_text = outbound_quality_pass(
                    _out_text,
                    chat_id=getattr(getattr(original_message, "chat", None),
                                    "id", None),
                    persona_name=self._persona_display_name())
            except Exception:
                pass
            # #105/#106（实施91）：A 线原生回复的收口点守卫余下两连——呼格纠正
            # + 铆定语言兜底（混语已在 outbound_quality_pass 内）。A 线不经
            # 编排器，orch.send 的收口点罩不到这条链（#106 击穿机制：B67
            # 「发→英」在 A 线从无消费点，图片轮三连纯中文直发英文铆定会话）。
            # 开关随 companion.outbound_text_guard.{vocative,lang_pin}。
            try:
                from src.ai.outbound_text_guard import resolve_cfg as _sp_cfg
                _sp_conf = getattr(self, "config", None) or {}
                _spg = _sp_cfg(_sp_conf)
                _sp_chat = str(getattr(
                    getattr(original_message, "chat", None), "id", "") or "")
                _sp_acct = str(
                    getattr(self, "account_id", "default") or "default")
                if _spg.get("enabled", True) and _spg.get("vocative", True):
                    from src.ai.sendpoint_guard import (
                        resolve_sendpoint_names, sendpoint_vocative_pass)
                    _nm = resolve_sendpoint_names(
                        _sp_conf, "telegram", _sp_acct, _sp_chat)
                    if _nm:
                        _vt, _vm = sendpoint_vocative_pass(_out_text, _nm)
                        if _vt != _out_text:
                            self.logger.warning(
                                "[sendpoint] A 线呼格已纠正（#105/#96）"
                                "chat=%s: swap=%s near=%s self=%s",
                                _sp_chat, _vm.get("swap_hits"),
                                _vm.get("near_hits"),
                                _vm.get("self_voc_hits"))
                            _out_text = _vt
                if _spg.get("enabled", True) and _spg.get("lang_pin", True):
                    from src.ai.sendpoint_guard import sendpoint_lang_pin_fix
                    _pt, _pact = await sendpoint_lang_pin_fix(
                        "telegram", _sp_acct, _sp_chat, _out_text)
                    if _pt is None:
                        # HOLD：发错语言比不发更糟（无兜底纪律）；本条放弃。
                        self.logger.warning(
                            "[sendpoint] A 线铆定语言 HOLD，本条不发 chat=%s: %r",
                            _sp_chat, _out_text[:60])
                        return
                    if _pt != _out_text:
                        _out_text = _pt
            except Exception:
                self.logger.debug("[sendpoint] A 线收口点守卫异常（原样放行）",
                                  exc_info=True)
            # WP-4 rider ① 系统级披露（compliance.disclosure.notice，基线关）：
            # 每会话首条 AI 出站前置披露语，持久防重键=conv_id（与 B 线 autosend /
            # 协议线/主动触达同键空间——任一条线先披露过，其余线不再重复）。刻意
            # 放在质量管道之后（披露语是系统文案，不过改写/复读检测），且只挂文本
            # 发送口（克隆声绝不念披露语；语音先行的会话由首条文本回复补披露，
            # 标记只在真应用时才烧）。分块发送天然只中首块（首块烧标记后
            # 同回复后续块查标记即跳过）。任何异常＝原样发送，绝不阻断回复。
            try:
                from src.compliance.disclosure import apply_disclosure_for
                from src.inbox.normalizer import conv_id as _dc_conv_id
                _dc_store = None
                try:
                    from src.integrations.protocol_bridge import get_inbox_store
                    _dc_store = get_inbox_store()
                except Exception:
                    _dc_store = None
                _out_text, _ = apply_disclosure_for(
                    _dc_conv_id(
                        "telegram",
                        str(getattr(self, "account_id", "default") or "default"),
                        str(original_message.chat.id)),
                    _out_text, store=_dc_store)
            except Exception:
                self.logger.debug("[披露] 注入异常（原样发送）", exc_info=True)
            _rt = self._reply_to_message_id_for_send(original_message)
            send_kw: Dict[str, Any] = dict(
                chat_id=original_message.chat.id,
                text=_out_text,
            )
            if _rt is not None:
                send_kw["reply_to_message_id"] = _rt
            if parse_mode is not None:
                send_kw["parse_mode"] = parse_mode
            _sent = await self.client.send_message(**send_kw)
            # 统一发送后记账（与 send_photo 共用）：刷新墙钟 + 记入共用发送计数器
            # （喂反封号闸门 + 机群健康灯今日外发量，best-effort 绝不阻断发送）。
            self._postsend_record_count()
            # #88：A 线自动回复送达成功也要清 dead-peer 标——此前只有主动外发/
            # 手动路由清，自动回复一直在送达、黄条却赖着不走（Yhang 实锤）。
            self._dead_peer_clear_on_delivery(original_message.chat.id)
            # N4b 出站镜像（坐席台）+ Q3 contacts 外发互动（mutuality）——与富媒体共用一处。
            # 带回真实 message.id 作幂等键，乐观镜像行与回显共用主键 → 精确去重。
            self._postsend_mirror_and_record(
                original_message.chat.id, _out_text,
                msg_id=getattr(_sent, "id", "") or "")
            if getattr(original_message, 'from_user', None) and getattr(original_message.from_user, 'id', None):
                self._record_session_reply(original_message.chat.id, original_message.from_user.id)
                self._record_auto_reply(original_message.chat.id, original_message.from_user.id)
                if getattr(self, 'four_layer_trigger', None):
                    self.four_layer_trigger.update_cooldown(
                        f"group_{original_message.chat.id}",
                        str(original_message.from_user.id),
                    )
            # 打真实发出的文本（out_text 可能被质量管道改写过）——
            # 2026-07-26 排障实锤：这里打 reply_text 原文，与镜像/对端看到的不一致，误导排查。
            self.logger.info("已回复消息: %s", self._log_safe_text(_out_text))
        except Exception as e:
            self.logger.error("发送回复失败: %s", e)
            self._handle_send_exc(e)   # G2 分级急停 + 实施31 TG 告警（best-effort）

    @staticmethod
    def _is_peer_invalid_error(exc: Any) -> bool:
        """「本地不认识这个 peer」类错误——**不是**平台风控信号。

        三种形态（2026-07-27 双号群演灰度全部实录）：
        - ``utils.get_peer_type`` 的 ``ValueError("Peer id invalid: …")``
          （本地 id 边界校验；2.0.106 撞超 int32 群 id 时抛它）；
        - RPC ``PeerIdInvalid``（服务器 400 PEER_ID_INVALID）；
        - RPC ``ChannelInvalid``（400 CHANNEL_INVALID by channels.GetChannels）
          ——resolve 缓存 miss 后 pyrogram 拿 ``access_hash=0`` 去要 peer 被拒
          的直接症状，dialogs 预热拿到真 access_hash 即愈。
        刻意**不含** ``ChannelPrivate``（CHANNEL_PRIVATE=被踢/无权限，预热无意义）。
        """
        name = type(exc).__name__
        low = str(exc or "").lower()
        return ("PeerIdInvalid" in name or "ChannelInvalid" in name
                or "peer_id_invalid" in low or "peer id invalid" in low
                or "channel_invalid" in low)

    async def _warm_group_peer(self, chat_id: Any) -> bool:
        """两级预热让 pyrogram 把群 peer 写进 session 缓存（见到目标群 → True）。

        「号在群里但本地无 access_hash」时 ``get_chat`` 救不了自己（内部同样走
        ``resolve_peer``），标准姿势：
        ① ``get_dialogs``——响应带全量 peer，SQLite session 顺手入库、且**不经过**
          id 边界校验。刚有动静的新群必在 dialogs 前排，limit=60 足够；
        ② ①未见 → raw ``messages.GetAllChats``——返回**该号所在的全部群/频道**，
          无视文件夹/归档（新号常开「陌生会话自动归档」，被拉进群直接落 folder 1，
          get_dialogs 主列表根本看不见——2026-07-27 双号群演灰度实锤盲区）；
          响应 chats 经 ``fetch_peers`` 写库后即可正常 resolve。
        两级都未见 → 号确实不在群内（地面真相，调用方据此明确报错）。
        仅群/频道（负 id）值得预热：私聊 peer 不会出现在 dialogs 预热收益里。
        """
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return False
        if cid >= 0 or not self.client:
            return False
        try:
            async for dialog in self.client.get_dialogs(limit=60):
                c = getattr(dialog, "chat", None)
                if c is not None and getattr(c, "id", None) == cid:
                    return True
        except Exception:  # noqa: BLE001
            self.logger.debug("[peer预热] get_dialogs 失败", exc_info=True)
        return await self._warm_via_all_chats(cid)

    async def _warm_via_all_chats(self, cid: int) -> bool:
        """预热第 ② 级：raw GetAllChats 全量所在群写缓存（见到目标群 → True）。

        与 get_dialogs 的差异就是**归档不隐身**。响应按 raw 类型换算 bot-api id：
        Channel* → ``-100`` 前缀形态，Chat*（基础小群）→ 取负。fetch_peers 失败
        不阻断命中判定（缓存没写上，重发仍会失败，但「在不在群」的答案是真的）。
        """
        try:
            from pyrogram import raw, utils as _pu
        except Exception:  # noqa: BLE001 —— 纯单测无 pyrogram 环境
            return False
        try:
            r = await self.client.invoke(
                raw.functions.messages.GetAllChats(except_ids=[]))
        except Exception:  # noqa: BLE001
            self.logger.debug("[peer预热] GetAllChats 失败", exc_info=True)
            return False
        chats = list(getattr(r, "chats", None) or [])
        if chats:
            try:
                await self.client.fetch_peers(chats)
            except Exception:  # noqa: BLE001
                self.logger.debug("[peer预热] fetch_peers 失败", exc_info=True)
        for c in chats:
            raw_id = getattr(c, "id", None)
            if raw_id is None:
                continue
            tname = type(c).__name__
            try:
                if tname.startswith("Channel"):
                    bot_id = _pu.get_channel_id(int(raw_id))
                elif tname.startswith("Chat"):
                    bot_id = -int(raw_id)
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if bot_id == cid:
                self.logger.info("[peer预热] GetAllChats 命中群 %s（主列表未见，"
                                 "疑似归档/文件夹）", cid)
                return True
        return False

    async def ensure_group_peer(self, chat_id: Any) -> bool:
        """群 peer 可达性体检（供开演前 preflight 等前置校验）：True=此号能对群发言。

        先直查 ``resolve_peer``（缓存热=零 RPC），冷则走两级预热再复核。
        非负 id（私聊/username）不属本检查范围，恒 True 交给发送路径。
        群演场景专用语义：**False 就是「号不在群/不可达」的确定结论**，
        调用方应当拦下而不是带病开演。
        """
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return True
        if cid >= 0:
            return True
        if not self.client:
            return False
        try:
            await self.client.resolve_peer(cid)
            return True
        except Exception:  # noqa: BLE001 —— 缓存冷/边界拦截，进预热
            pass
        if not await self._warm_group_peer(cid):
            return False
        try:
            await self.client.resolve_peer(cid)
            return True
        except Exception:  # noqa: BLE001
            self.logger.debug("[peer预热] 预热后 resolve 仍失败 %s", cid, exc_info=True)
            return False

    async def _retry_send_after_peer_warmup(self, chat_id: Any, text: str):
        """peer-invalid 自愈：dialogs 预热 → 原样重发一次。失败返回 None（绝不抛）。

        重试抛出的**新**异常若已不是 peer 类（FloodWait/封号特征等）→ 照常喂
        G2 风控分级，不因走了自愈岔路而漏报真风控。
        """
        try:
            if not await self._warm_group_peer(chat_id):
                self.logger.warning("[peer预热] dialogs 未见群 %s（号不在群内？）", chat_id)
                return None
            return await self.client.send_message(chat_id, text)
        except Exception as e2:  # noqa: BLE001
            self.logger.warning("[peer预热] 重试仍失败 %s: %s", chat_id, e2)
            if not self._is_peer_invalid_error(e2):
                self._handle_send_exc(e2)
            return None

    def _dead_peer_clear_on_delivery(self, chat_id: Any) -> None:
        """#88：真实送达=可达铁证 → 清 dead-peer 标（gated；未标/未启用 no-op）。

        0830 skuio 实锤（工单 #88）：#73 的清标只挂了 ``_send_text_guarded``
        （主动外发）与手动路由——A 线**自动回复**（``_send_reply``）与媒体直发
        （send_photo / send_voice_file）送达成功时标记纹丝不动 → Yhang 会话消息
        18:38 双勾送达、黄条「曾被对方拉黑」仍常驻。任何真实送达路径都必须清。
        """
        try:
            _on, _reg = self._dead_peer_guard()
            if _on and _reg is not None and _reg.unblock("telegram", chat_id):
                self.logger.info(
                    "[dead-peer] 送达成功，已解除 %s 的不可达标记", chat_id)
        except Exception:
            pass

    def _dead_peer_guard(self):
        """死 peer 登记表守卫（gated on ``ops.dead_peer_registry.enabled``，默认关）。

        返回 ``(enabled, registry)``——**全程吞异常**，任何解析失败 → ``(False, None)``
        恒放行（守卫自身绝不阻断发送，与 ``_presend_blocked`` 同承诺）。落盘位与
        proactive 的 bad_peers 同目录（实例 ``config/dead_peers.json``）。
        """
        try:
            from pathlib import Path as _P

            from src.ops.dead_peer_registry import (
                dead_peer_enabled, get_dead_peer_registry,
            )
            cfgobj = getattr(self, "config", None)
            if not dead_peer_enabled(cfgobj):
                return False, None
            path = None
            cp = getattr(cfgobj, "config_path", None)
            if cp:
                path = str(_P(cp).parent / "dead_peers.json")
            cfg = getattr(cfgobj, "config", None)
            if not isinstance(cfg, dict):
                cfg = cfgobj if isinstance(cfgobj, dict) else {}
            _dpc = ((cfg.get("ops") or {}).get("dead_peer_registry") or {})
            ttl = float((_dpc.get("ttl_sec") or 0) or 0)
            # reason 级 TTL（可选）：注销恒永久；被拉黑/群禁言配 TTL 到期可探路重试
            _tbr = _dpc.get("ttl_by_reason")
            ttl_by_reason = _tbr if isinstance(_tbr, dict) else None
            return True, get_dead_peer_registry(
                path=path, ttl_sec=ttl, ttl_by_reason=ttl_by_reason)
        except Exception:
            return False, None

    async def _send_text_guarded(self, chat_id: int, text: str):
        """A 线外发文本核心：过发送前护栏 + 节流 + 记账，返回 ``(ok, sent_message)``。

        - ``ok``：是否成功送出（过护栏且未抛；与旧 ``send_message`` 的 bool 语义一致）。
        - ``sent_message``：底层 ``client.send_message`` 的返回（真实 pyrogram 为 ``Message``，
          可取 ``.id``；测试桩/无返回时为 None）。

        **不**做出站镜像（避免与编排器中心化收件箱回写重复镜像）。
        """
        # 死 peer 闸（gated 默认关）：对已确证永久不可达的 peer（账号注销/被拉黑）
        # 直接跳过——修「同一死号每 15min 重发、2h ×17 次累积风控」（2026-07-27 实锤）。
        _dp_on, _dp_reg = self._dead_peer_guard()
        if _dp_on and _dp_reg is not None and _dp_reg.is_blocked("telegram", chat_id):
            self.logger.info(
                "[dead-peer] 跳过已拉黑 peer %s（%s，避免无效重发累积风控）",
                chat_id, _dp_reg.reason_of("telegram", chat_id) or "permanent")
            return False, None
        try:
            # 统一发送前护栏：G1 Kill-Switch + N 线反封号闸门（与 _send_reply/send_photo 共用）
            if self._presend_blocked(peer=chat_id):
                return False, None
            await self._presend_pace()
            if not self.client:
                self.logger.error("客户端未初始化")
                return False, None
            _sent = await self.client.send_message(chat_id, text)
            self._postsend_record_count()
            # #73/#88：真实送达=可达铁证 → 清 dead-peer 标（覆盖 TTL 放行探路
            # 成功后的复位；未标记/未启用时 no-op）。
            self._dead_peer_clear_on_delivery(chat_id)
            self.logger.info("已发送消息到 %s: %s...", chat_id, text[:50])
            return True, _sent
        except Exception as e:
            # 群聊 peer 冷启动自愈（2026-07-27 双号群演灰度实锤）：新号第一次对群
            # 开口时本地缓存缺失 → resolve 失败，dialogs 预热后原样重发一次即活。
            # peer-invalid 是本地缓存/边界问题**不是风控**——成败都不喂 ban_signal
            # （PeerIdInvalid 在 _PAUSE_NAMES 里，误喂＝无辜冻号 60min，正是此前
            # proactive 主账号被 kill-switch 冻 1h 事故的同款机制）。
            if self._is_peer_invalid_error(e):
                _sent2 = await self._retry_send_after_peer_warmup(chat_id, text)
                if _sent2 is not None:
                    self._postsend_record_count()
                    self.logger.info("已发送消息到 %s（peer 预热重试）: %s...",
                                     chat_id, str(text or "")[:50])
                    return True, _sent2
                self.logger.error("发送消息失败（peer 预热后仍不可达）: %s", e)
                return False, None
            self.logger.error("发送消息失败: %s", e)
            # 死 peer 登记（gated）：永久不可达（账号注销/被拉黑/写禁止）落共享黑名单
            # → 下次本方法开头的闸直接拦截。可自愈类（peer_invalid，上面已走预热分支）
            # classify 归 peer_unresolved，record 内部按「非永久」忽略，双重保险不误拉黑。
            if _dp_on and _dp_reg is not None:
                try:
                    from src.ops.dead_peer_registry import classify_send_error
                    _rsn = classify_send_error(e)
                    # #73 溯源：evidence 记原始错误摘要——「这个标哪来的」可回答
                    if _rsn and _dp_reg.record("telegram", chat_id, _rsn,
                                               evidence=str(e)[:160]):
                        self.logger.info(
                            "[dead-peer] 已拉黑 %s（%s，永久不可达不再重发）", chat_id, _rsn)
                except Exception:
                    pass
            self._handle_send_exc(e)   # G2 分级急停 + 实施31 TG 告警（best-effort）
            return False, None

    async def send_message(self, chat_id: int, text: str) -> bool:
        """A 线主动外发文本（主动问候/唤醒/关怀/编排器受管 worker 都经此）。

        Stage M：此前是裸 Pyrogram 调用，绕过 Kill-Switch/反封号/节流——成为旁路风控缺口
        （主动问候经 CompanionWorker.send→本方法 直发）。现统一走与 ``_send_reply`` 同一套发送前
        护栏 + 节流 + 记账。**不**做出站镜像（避免与编排器中心化收件箱回写重复镜像）。
        """
        ok, _ = await self._send_text_guarded(chat_id, text)
        return ok

    async def send_message_return_id(self, chat_id: int, text: str):
        """同 ``send_message``，但回传 ``(ok, msg_id)``——``msg_id`` 为发出的**真实**
        ``message.id``（无则空串）。

        P4-4：供 companion worker 把已读回执（``UpdateReadHistoryOutbox``）精确绑定到
        对应出站消息行；旧 ``send_message`` 只回 bool、丢弃了 id，导致 companion 手动发送的
        消息无法显示双勾。best-effort：失败/被拦 → ``(False, "")``。
        """
        ok, _sent = await self._send_text_guarded(chat_id, text)
        return ok, (str(getattr(_sent, "id", "") or "") if ok else "")

    async def send_photo(self, chat_id: Any, photo_path: str,
                         caption: str = "") -> bool:
        """A 线主客户端直发照片（Pyrogram send_photo）。供陪伴形象照「直发」缝。

        失败绝不抛、返回 False（调用方退回文字陪伴）；命中风控走 G2 封号信号分级处置。
        """
        try:
            if not self.client:
                self.logger.error("客户端未初始化")
                return False
            if not photo_path:
                return False
            # 统一发送前护栏（与文本回复共用）：冻结/被反封号闸门拦 → 不发，避免图绕过风控。
            if self._presend_blocked(peer=chat_id):
                self.logger.info("照片发送被发送前护栏拦截，跳过（chat=%s）", chat_id)
                return False
            # 统一节流：与文本共用墙钟，图文混发也排队（不瞬时双发触发反垃圾）。
            await self._presend_pace()
            # 反封号·去重微扰（默认关，opt-in）：A 线自拍/形象照直发多号 → 文件哈希相同是
            # 垃圾信号。发送前产「视觉无差、字节唯一」临时副本发出、发完删；软失败回落原图。
            # 与编排器 send_media 同一 outbound_media.dedup 语义——A 线是绕过编排器的直发缝，
            # 这里补上覆盖（同时罩住 companion worker / skill_manager 的照片直发兜底）。
            _send_path, _dedup_temp = photo_path, False
            try:
                from src.integrations.shared.media_dedup import perturb_for_send
                _raw_cfg = self.config.config if hasattr(self.config, "config") else {}
                _send_path, _dedup_temp = perturb_for_send(photo_path, "image", _raw_cfg)
            except Exception:
                _send_path, _dedup_temp = photo_path, False
            try:
                _sent = await self.client.send_photo(chat_id, _send_path, caption=caption or "")
            finally:
                try:
                    from src.integrations.shared.media_dedup import cleanup_temp
                    cleanup_temp(_send_path, _dedup_temp)
                except Exception:
                    pass
            # 统一记账：刷新墙钟 + 记入共用计数器（照片也计入今日外发量，反封号不漏算）。
            self._postsend_record_count()
            self._dead_peer_clear_on_delivery(chat_id)   # #88 媒体送达同清标
            # 出站镜像 + contacts 记账：坐席台看见「AI 发了图」、亲密度计入这次外发。
            # 带 media_ref → 工作台渲染成**真图**而不只是一行「[图片] 配文」；
            # 发布用 canonical 原图（不是去重微扰出的临时副本，那个已被删掉）。
            _cap = (caption or "").strip()
            _mt, _mref = self._publish_media_ref(photo_path)
            # 镜像正文＝干净配文（[图片] 语义由 media_type 承载，与 B 线 caption 同
            # 口径，气泡里不再重复方括号标记）；contacts 预览保留标记表意。
            self._postsend_mirror_and_record(
                chat_id, f"[图片] {_cap}".strip() if _cap else "[图片]",
                msg_id=getattr(_sent, "id", "") or "",
                media_type=_mt or "image", media_ref=_mref,
                mirror_text=_cap)
            self.logger.info("已发送照片到 %s（%s）", chat_id, photo_path)
            return True
        except Exception as e:
            self.logger.error("发送照片失败: %s", e)
            self._handle_send_exc(e)   # G2 分级急停 + 实施31 TG 告警（best-effort）
            return False

    async def send_voice_file(self, chat_id: Any, voice_path: str,
                              caption: str = "", mirror_note: str = "") -> bool:
        """A 线主客户端直发**现成语音文件**（预渲染唱段等；实施66 P0-1 直发缝）。

        与 ``send_photo`` 同一套发送前护栏/节流/外发记账/出站镜像——A 线绕过
        编排器的媒体直发缝必须自带风控四件套，否则唱段成旁路。转码/跨 loop
        由 ``voice_sender`` 承担（备货是标准 OggS+OpusHead → 魔数放行零重编码）。
        失败绝不抛、返回 False（调用方回落文字）。``mirror_note``＝收件箱镜像
        正文（如 ``[唱歌]《月光》♪ 首句``），缺省 ``[语音]``。
        """
        try:
            if not self.client:
                self.logger.error("客户端未初始化")
                return False
            if not voice_path:
                return False
            if self._presend_blocked(peer=chat_id):
                self.logger.info("语音文件发送被发送前护栏拦截，跳过（chat=%s）", chat_id)
                return False
            await self._presend_pace()
            from src.client.voice_sender import (
                probe_audio_duration_ms, send_telegram_voice,
            )
            _dur_ms = probe_audio_duration_ms(voice_path)
            # 变量名刻意不以 sent 结尾：test_outbound_media_mirror 的归档窗口钉
            # 按「sent =」+发送调用的**首现子串**定锚，本方法位置靠前撞名会抢锚。
            _msg = await send_telegram_voice(
                self.client, chat_id, voice_path,
                duration=(max(1, int(_dur_ms / 1000)) if _dur_ms else None),
                caption=(caption or None))
            if not _msg:
                return False
            # 发送已成功——之后的记账/镜像失败绝不能把「已送达」误报成 False
            # （调用方会回落文字=客户收到双份）。
            self._dead_peer_clear_on_delivery(chat_id)   # #88 语音送达同清标
            try:
                self._postsend_record_count()
                _note = (mirror_note or "").strip() or "[语音]"
                _mt, _mref = self._publish_media_ref(voice_path)
                self._postsend_mirror_and_record(
                    chat_id, _note,
                    msg_id=str(getattr(_msg, "id", "") or ""),
                    media_type="voice", media_ref=_mref,
                    mirror_text=(caption or "").strip())
            except Exception:
                self.logger.debug("语音文件镜像/记账失败（送达不受影响）",
                                  exc_info=True)
            self.logger.info("已发送语音文件到 %s（%s）", chat_id, voice_path)
            return True
        except Exception as e:
            self.logger.error("发送语音文件失败: %s", e)
            self._handle_send_exc(e)   # G2 分级急停（与图/文同口径）
            return False

    async def _send_escalation_private_jump_hint(
        self,
        peer: Any,
        spec: Dict[str, Any],
        message_id: int,
        *,
        after_forward_ok: bool,
    ) -> None:
        """
        私聊内追加一条「可点击定位」说明：HTML 正文 + 内联按钮（t.me 或 tg://openmessage）。
        解决仅靠转发条在部分客户端无法跳回群内指定消息的问题。
        """
        if not self.client:
            return
        he_cfg = (self.config.get("human_escalation") or {}) if self.config else {}
        if not bool(he_cfg.get("forward_private_jump_hint", True)):
            return
        from src.utils.human_escalation import build_telegram_message_link

        from_chat_id = spec.get("from_chat_id")
        chat_username = spec.get("chat_username")
        chat_title = (spec.get("chat_title") or "").strip()
        url = build_telegram_message_link(
            from_chat_id, int(message_id), chat_username
        )

        try:
            from pyrogram.enums import ParseMode
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            ParseMode = None  # type: ignore
            InlineKeyboardButton = None  # type: ignore
            InlineKeyboardMarkup = None  # type: ignore

        if after_forward_ok:
            head = (
                "👆 上一条为<strong>群内用户原话</strong>（转发）。\n"
                "若转发预览无法点进群里，请用下方<strong>按钮</strong>或<strong>链接</strong>直达该条消息。"
            )
        else:
            head = (
                "⚠️ 未能转发群内原消息到私聊，请用下方<strong>按钮</strong>或<strong>链接</strong>"
                "进入群内查看对应话术。"
            )
        parts: List[str] = [head]
        if chat_title:
            parts.append(f"群：{html.escape(chat_title)}")
        parse_mode = ParseMode.HTML if ParseMode else None
        reply_markup = None

        if url:
            parts.append(
                f'直达消息：<a href="{html.escape(url, quote=True)}">打开 #msg{message_id}</a>'
            )
            if InlineKeyboardMarkup and InlineKeyboardButton:
                try:
                    reply_markup = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📍 打开群内该条消息", url=url
                                )
                            ]
                        ]
                    )
                except Exception:
                    reply_markup = None
            body = "\n".join(parts)
            try:
                await self.client.send_message(
                    chat_id=peer,
                    text=body,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                self.logger.info(
                    "人工转接: 已向客服 peer=%s 发送私聊定位提示 msg_id=%s",
                    peer,
                    message_id,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 私聊定位提示(HTML)失败 peer=%s: %s，尝试纯文本",
                    peer,
                    e,
                )
                try:
                    await self.client.send_message(
                        chat_id=peer,
                        text=f"打开群内消息：\n{url}",
                    )
                except Exception as e2:
                    self.logger.warning(
                        "人工转接: 私聊定位纯文本也失败 peer=%s: %s", peer, e2
                    )
        else:
            tail = (
                "当前无法生成 t.me / openmessage 直达链接（例如非标准会话 id）。\n"
                "请点按上一条「转发」顶栏进入群，或向管理员索取群邀请链接。"
            )
            try:
                await self.client.send_message(
                    chat_id=peer,
                    text="\n".join(parts + [tail]),
                    parse_mode=parse_mode,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 私聊定位说明(无 URL)失败 peer=%s: %s", peer, e
                )

    async def _maybe_send_voice_reply(
        self,
        original_message,
        reply_text: str,
        *,
        is_peer_voice: bool = False,
        peer_audio_emotion: Optional[Dict[str, Any]] = None,
        fail_state: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Try to send a TTS voice note for *reply_text*.

        Returns ``True`` if a voice note was sent (caller should skip text send).
        Returns ``False`` if voice was skipped/failed (caller sends text normally).

        ``fail_state``（2026-08-02，可选 out 参数）：语音**被触发且尝试后失败**
        的出口会写入 ``{"synth_failed": True, "reason": ...}``——返回值语义不变
        （False=调用方发文字），调用方据此做「诚实回落」（剥语音承诺句）。
        trigger 未命中/策略判文字的早退不写。

        Trigger modes (``telegram.voice_reply.trigger``):
        - ``when_peer_voice`` — only when the incoming message was a voice note
        - ``always``          — every reply
        - ``random``          — with configurable probability
        - ``smart``           — context-aware fitness scoring (shared ai.voice_fitness)
        - ``never``           — effectively disables (same as ``enabled: false``)
        """
        try:
            raw_cfg = self.config.config if hasattr(self.config, "config") else {}
            vr_cfg: Dict[str, Any] = (raw_cfg.get("telegram") or {}).get("voice_reply") or {}
            from src.client.voice_sender import resolve_opus_application
            _opus_app = resolve_opus_application(raw_cfg)
            if not vr_cfg.get("enabled", False):
                self.logger.warning("[voice_reply] skip: enabled=false (section=%s)", "found" if vr_cfg else "missing")
                return False
            # B120（实施74）：全局「启用语音回复」显式关闭 → A 线自动语音同样
            # 禁声（统一闸；telegram.voice_reply.enabled 是本链自己的开关，
            # 全局显式关时两者取 AND——键缺席不影响存量行为）。
            from src.inbox.voice_autosend import global_voice_reply_off
            if global_voice_reply_off(raw_cfg):
                self.logger.info(
                    "[voice_reply] skip: 全局「启用语音回复」显式关闭（B120 统一闸）")
                return False
            # 报障群/支持账号语音压制（bug_intake，2026-08-18）：支持人设没有
            # 克隆声，放行会落到全局 voice_profile 的陪伴参考音（错声=「换人」
            # 级穿帮）；报障群场景也不该有语音条。
            try:
                from src.ops.bug_intake import voice_suppressed
                if voice_suppressed(
                        raw_cfg,
                        getattr(getattr(original_message, "chat", None),
                                "id", ""),
                        getattr(self, "account_id", "")):
                    self.logger.info("[voice_reply] skip: bug_intake 压制")
                    return False
            except Exception:
                pass

            trigger = str(vr_cfg.get("trigger", "when_peer_voice")).strip().lower()
            if trigger == "never":
                self.logger.debug("[voice_reply] skip: trigger=never")
                return False
            # A3 熔断（#150）：同会话短窗语音达阈值 → 本条降级文字并冷却（A 线与
            # B 线同口径；JZPBAC 实锤 A 线 voice_reply 正是乒乓的发送侧）。
            try:
                from src.client.voice_burst_guard import voice_degrade_reason
                _burst_why = voice_degrade_reason(
                    getattr(getattr(original_message, "chat", None), "id", ""),
                    vr_cfg)
            except Exception:
                _burst_why = ""
            if _burst_why:
                self.logger.warning(
                    "[voice_reply] skip: %s（语音连发熔断，本条降级文字）", _burst_why)
                return False
            # 客户**打字**点名要语音/唱歌（when_peer_voice 只认「对方发了语音」会漏）→
            # 强制语音（P0-5，2026-07-29 对练补漏：打字要语音得不到语音、AI 打字冒充唱歌）。
            _peer_req_voice = False
            try:
                from src.ai.outbound_promise_guard import wants_media as _wm_v
                _msg_txt = (getattr(original_message, "text", None)
                            or getattr(original_message, "caption", None) or "")
                _peer_req_voice = (_wm_v(str(_msg_txt)) == "voice")
            except Exception:
                _peer_req_voice = False
            # 语音欠账兑现（voice_iou，默认关）：上一轮诚实回落许过「回头补给
            # 你」→ 本轮视同客户点名要语音强制尝试合成（trigger=never 已在上方
            # 早退，不受影响）；真发成功后 _note_voice_ok 里清账，失败保留欠账
            # TTL 内下次再试（不重复记账，防覆盖时间戳永不过期）。
            _iou_pending = False
            _iou_key = ""
            try:
                from src.client.voice_iou import (
                    parse_iou_cfg as _iou_cfg_fn,
                    pending_iou as _iou_pending_fn,
                )
                _iou_cfg = _iou_cfg_fn(raw_cfg)
                if _iou_cfg["enabled"]:
                    _iou_key = str(getattr(
                        getattr(original_message, "chat", None), "id", "")
                        or "")
                    if _iou_key and _iou_pending_fn(
                            _iou_key, ttl_hours=_iou_cfg["ttl_hours"]):
                        _iou_pending = True
                        if not _peer_req_voice:
                            _peer_req_voice = True
                            self.logger.info(
                                "[voice_iou] 欠账待补 → 本轮视同点名要语音 "
                                "chat=%s", _iou_key)
            except Exception:
                _iou_pending = False
            if (trigger == "when_peer_voice" and not is_peer_voice
                    and not _peer_req_voice):
                self.logger.debug("[voice_reply] skip: trigger=when_peer_voice but msg is not voice")
                return False
            if trigger == "random" and not _peer_req_voice:
                prob = float(vr_cfg.get("probability", 0.3) or 0.3)
                if random.random() >= prob:
                    return False
            if trigger == "smart" and not _peer_req_voice:
                # 与 System Z autosend 同源的上下文感知评分（消除重复决策逻辑）。原生 TG
                # 路径暂只喂「回复情绪 + 对等」信号（频率/客户情绪可后续接入）；内容/长度
                # 硬否决与 autosend 完全一致。低分/不达标 → 回落文本。
                from src.ai.voice_fitness import voice_fitness
                _smart = vr_cfg.get("smart") if isinstance(vr_cfg.get("smart"), dict) else {}
                _merged = {
                    "max_chars": int(vr_cfg.get("max_text_chars", 220) or 220), **_smart}
                _dec = voice_fitness(
                    (reply_text or "").strip(),
                    peer_sent_voice=is_peer_voice, cfg=_merged)
                if not _dec.send_voice:
                    self.logger.debug(
                        "[voice_reply] skip: smart fitness=%s (%s)", _dec.score, _dec.reason)
                    return False

            max_chars = int(vr_cfg.get("max_text_chars", 220) or 220)
            # 客户点名要语音时放宽上限到硬帽（~30s；避免"要了语音却因超长回落文字"）
            if _peer_req_voice:
                max_chars = max(max_chars, 300)
            clean_text = (reply_text or "").strip()
            if not clean_text or len(clean_text) > max_chars:
                self.logger.debug(
                    "[voice_reply] skipped: text len=%d max=%d", len(clean_text), max_chars
                )
                return False

            # 统一发送前护栏（与文本/照片共用）：冻结/被反封号闸门拦 → 不出语音、也不白跑 TTS。
            # 返回 False → 调用方回退文本 _send_reply，文本同样会被护栏拦 → 冻结期彻底静默。
            if self._presend_blocked(
                    peer=getattr(getattr(original_message, "chat", None),
                                 "id", None)):
                self.logger.info("[voice_reply] skip: 发送前护栏拦截（kill-switch/反封号闸门）")
                return False

            # ── 语音断档台账 + 失败事实回传（2026-08-02）─────────────────────
            # 到这里=语音已被触发；此后「尝试合成但最终回落文字」的每个出口
            # 都要记账（低流量断档看门狗 voice_outage 的判据）+ 把失败写回
            # fail_state（调用方据此做诚实回落，剥「语音这就来」类空头支票）。
            # 上方 trigger/长度/护栏早退不记。record=False 用于「决定层拒发」
            # （如语言不匹配）：要诚实回落但不算合成断档——外语会话拒发是
            # 能力缺口不是故障，混进台账会把断档告警刷成假阳性。
            def _note_voice_fail(reason: str, *, record: bool = True) -> None:
                if fail_state is not None:
                    try:
                        fail_state["synth_failed"] = True
                        fail_state["reason"] = str(reason or "")
                    except Exception:
                        pass
                if not record:
                    return
                try:
                    from src.ai.voice_outage import get_voice_outage
                    get_voice_outage().record_voice_attempt(
                        False, "aline", str(reason or ""))
                except Exception:
                    pass

            def _note_voice_ok() -> None:
                try:
                    from src.ai.voice_outage import get_voice_outage
                    get_voice_outage().record_voice_attempt(True, "aline")
                except Exception:
                    pass
                # 语音欠账已补（voice_iou）：本函数是「语音真发成功」的唯一
                # 汇聚点（整段/分条/text-first 后台补发都经此）→ 在此清账。
                if _iou_pending and _iou_key:
                    try:
                        from src.client.voice_iou import clear_iou as _iou_clear
                        _iou_clear(_iou_key)
                        self.logger.info(
                            "[voice_iou] 欠账已补 chat=%s", _iou_key)
                    except Exception:
                        pass

            # 发图 GPU 占用中 defer（与 B 线 autosend 同口径，继承全局 avatar_voice.policy）
            from src.inbox.voice_autosend import resolve_defer_during_image
            if resolve_defer_during_image(raw_cfg, vr_cfg):
                try:
                    from src.inbox.image_autosend import image_gen_inflight
                    if image_gen_inflight() > 0:
                        self.logger.info(
                            "[voice_reply] skip: image generation in flight")
                        return False
                except Exception:
                    pass

            # 拟人已读回执（先看 → 挂「录音中」→ 语音）：与文本路径同口径。
            await self._mark_peer_read(
                getattr(getattr(original_message, "chat", None), "id", None))

            # 生成层口语版（Phase G）：生成时 LLM 已同步产出的「说话版」——按
            # 书面文本哈希取用（任何后处理改过文本 → 取不到 → 走既有口语化链）。
            # 命中则口语版直接送 TTS，跳过 TTS 前二次改写（省一次本地 LLM 往返）。
            _spoken = None
            try:
                from src.ai.spoken_variant import take_spoken_variant
                _spoken = take_spoken_variant(
                    reply_text,
                    scope=str(getattr(self, "account_id", "") or ""),
                )
            except Exception:
                _spoken = None
            synth_source = _spoken or clean_text
            if _spoken:
                self.logger.info(
                    "[voice_reply] 命中生成层口语版（len=%d→%d）",
                    len(clean_text), len(_spoken))

            # P3：端用户身份（私聊 chat.id 即对端 user_id）→ 会员档分层路由 TTS 后端
            # （VIP→旗舰，免费→降级省成本）。monetization 未就绪 → tier=None → 不路由。
            try:
                _contact_key = str(original_message.chat.id)
            except Exception:
                _contact_key = None
            _acc_pid = (
                self.account_persona_ids[0]
                if getattr(self, "account_persona_ids", None)
                else ""
            )
            from src.ai.persona_voice import resolve_effective_voice_context
            voice_ctx = resolve_effective_voice_context(
                raw_cfg, chat_key=_contact_key, account_persona_id=_acc_pid,
                contact_key=_contact_key, platform="telegram",
                account_id=getattr(self, "account_id", None), text=clean_text,
                peer_audio_emotion=peer_audio_emotion)
            voice_cfg = voice_ctx.get("voice_cfg") or {}
            # 语言路由（粤语 + follow_text 音色跟随文本语种）：与 B 线 voice_autosend
            # 同口径。主动选择而非兜底降级 → 命中路由后解除 no_edge 拒发；文本语种
            # 明确但无音色可映射 → 拒发语音回落文字（发错语言的语音比不发更糟）。
            _lang_route = ""
            try:
                from src.ai.lang_voice_route import (
                    is_reject_tag, route_voice_cfg_for_text)
                voice_cfg, _lang_route = route_voice_cfg_for_text(
                    voice_cfg, synth_source, raw_cfg)
                if is_reject_tag(_lang_route):
                    self.logger.info(
                        "[voice_reply] 语言不匹配拒发语音（%s）→ 回落文字",
                        _lang_route)
                    _note_voice_fail(
                        "lang_mismatch:" + str(_lang_route), record=False)
                    return False
            except Exception:
                self.logger.debug("[voice_reply] 语言路由异常（忽略）", exc_info=True)
            voice_cfg["enabled"] = True
            from src.inbox.voice_autosend import resolve_no_edge_fallback
            _no_edge = resolve_no_edge_fallback(raw_cfg, vr_cfg)
            if _lang_route:
                _no_edge = False
            if _no_edge:
                voice_cfg["fallback_on_error"] = False

            # ── Synthesize ──
            from src.ai.tts_pipeline import TTSPipeline

            tts = TTSPipeline(voice_cfg)
            timeout_sec = float(vr_cfg.get("timeout_sec", 30) or 30)

            # A1 text-first 与分条的错序防线（2026-07-27）：任一条语音已送达后，
            # 占位文字就不该再发（实锤日志：语音→「收到收到，马上回你」→语音）。
            _voice_progress = {"sent": False}

            def _note_voice_sent() -> None:
                _voice_progress["sent"] = True

            async def _voice_flow() -> bool:
                """合成→质检→发送全流程（A1 抽为内协程：可被 text-first 预算编排）。"""
                nonlocal synth_source
                # ── 分条发送（活人感）：长回复像真人一样连发 2-3 条短语音，条间按
                # 「按住录音的时长」留拟人间隔 + 挂"录音中"状态。全部合成成功才发
                # （节奏是表演出来的，不被 GPU 进度驱动）；任何失败回落单条整段路径。
                split_cfg = (vr_cfg.get("split_send")
                             if isinstance(vr_cfg.get("split_send"), dict) else {})
                # 整段先口语化一次（2026-07-27）：分条原本逐条各打一次 LLM 改写
                # （云端一次往返实测 ~5s ×N 条）→ 2 条即越过 text-first 25s 预算，
                # 客户先吃占位文字。生成层口语版已命中(_spoken)时整段本就是口语，
                # 无需再改；预处理失败 → 保持旧链逐条改写，行为不劣于旧版。
                _skip_llm_col = False
                if (not _spoken and split_cfg.get("enabled", False)
                        and len(synth_source) >= int(
                            split_cfg.get("min_total_chars", 24) or 24)):
                    _pre = await tts.prepass_colloquial_llm(
                        synth_source, spec=voice_ctx.get("emotion"),
                        colloquial_lead=True)
                    if _pre:
                        self.logger.info(
                            "[voice_reply] 整段口语化一次（len=%d→%d）→ 各条免打 LLM",
                            len(synth_source), len(_pre))
                        synth_source = _pre
                        _skip_llm_col = True
                if split_cfg.get("enabled", False):
                    # AI Live OS Agent4（dialogue_planner）：由「表演规划」Agent 决定分条 +
                    # **情绪缩放条间节奏**（兴奋短促连发 / 低落慢而长停顿，真人节奏带情绪）。
                    # planner 关或异常 → 回落原内联分条（gap_factor 用 split_cfg 静态值）。
                    parts = None
                    eff_split_cfg = split_cfg
                    try:
                        from src.ai.dialogue_planner import plan_voice_performance
                        _emo = voice_ctx.get("emotion")
                        _script = plan_voice_performance(
                            synth_source,
                            emotion=(getattr(_emo, "emotion", "neutral")
                                     if _emo else "neutral"),
                            intensity=(float(getattr(_emo, "intensity", 0.6) or 0.6)
                                       if _emo else 0.6),
                            split_cfg=split_cfg)
                        if _script.should_split:
                            parts = _script.part_texts
                            eff_split_cfg = {
                                **split_cfg,
                                "gap_factor": _script.gap_factor,
                                "gap_jitter_sec": list(_script.gap_jitter)}
                    except Exception:
                        parts = None
                    if parts is None and len(synth_source) >= int(
                            split_cfg.get("min_total_chars", 24) or 24):
                        from src.ai.voice_clone_client import pack_voice_parts
                        _p = pack_voice_parts(
                            synth_source,
                            part_max_chars=int(split_cfg.get("part_max_chars", 40) or 40),
                            max_parts=int(split_cfg.get("max_parts", 3) or 3),
                            min_tail_chars=int(split_cfg.get("min_tail_chars", 8) or 0))
                        if len(_p) >= 2:
                            parts = _p
                    if parts and len(parts) >= 2:
                        split_sent = await self._send_voice_reply_parts(
                            original_message, parts, tts, voice_ctx, vr_cfg, eff_split_cfg,
                            timeout_sec=timeout_sec, opus_application=_opus_app,
                            pre_colloquialized=bool(_spoken),
                            skip_llm_colloquial=_skip_llm_col,
                            on_part_sent=_note_voice_sent)
                        if split_sent:
                            _note_voice_ok()
                            if vr_cfg.get("send_text_summary", False):
                                await self._send_reply(original_message, reply_text)
                            return True
                        self.logger.info("[voice_reply] 分条路径未完成 → 回落整段单条")

                result = await tts.synthesize(
                    synth_source, timeout_sec=timeout_sec,
                    emotion=voice_ctx.get("emotion"),
                    pre_colloquialized=bool(_spoken),
                    skip_llm_colloquial=_skip_llm_col)
                if not result.ok:
                    self.logger.warning("[voice_reply] TTS failed: %s", result.error)
                    _note_voice_fail(str(result.error or "synth_failed"))
                    return False
                try:
                    from src.inbox.voice_autosend import should_reject_voice_tts_result
                    if should_reject_voice_tts_result(result, no_edge=_no_edge):
                        self.logger.warning(
                            "[voice_reply] no_edge_fallback 拒发 edge provider=%s "
                            "fallback_from=%s → 回落文字",
                            result.provider,
                            (result.extra or {}).get("fallback_from"))
                        try:
                            os.unlink(result.audio_path)
                        except Exception:
                            pass
                        _note_voice_fail("edge_rejected")
                        return False
                except Exception:
                    pass

                # ── Duration gate ──
                max_sec = float(vr_cfg.get("max_seconds", 60) or 60)
                if result.duration_sec > 0 and result.duration_sec > max_sec:
                    self.logger.warning(
                        "[voice_reply] audio %.1fs exceeds max %.1fs, fallback text",
                        result.duration_sec, max_sec,
                    )
                    try:
                        os.unlink(result.audio_path)
                    except Exception:
                        pass
                    _note_voice_fail("duration_exceeded")
                    return False

                # ── 质量闸门：截断/坏音（过短）→ 回落文字（宁缺毋滥）──
                from src.ai.tts_quality import looks_truncated, resolve_quality_gate
                _qg = resolve_quality_gate(vr_cfg)
                if _qg["enabled"]:
                    _bad, _why = looks_truncated(
                        synth_source, result.duration_sec,
                        min_sec_per_unit=_qg["min_sec_per_unit"],
                        min_units=_qg["min_units"])
                    if _bad:
                        self.logger.warning(
                            "[voice_reply] 整段疑似截断(%s) → 回落文字", _why)
                        try:
                            from src.ai.avatar_voice_stats import get_avatar_voice_stats
                            get_avatar_voice_stats().record_truncation_reject()
                        except Exception:
                            pass
                        try:
                            os.unlink(result.audio_path)
                        except Exception:
                            pass
                        _note_voice_fail("truncation_rejected:" + str(_why))
                        return False

                dur_int = int(result.duration_sec) if result.duration_sec > 0 else None
                _rt = self._reply_to_message_id_for_send(original_message)

                # ── Send voice ──
                from src.client.voice_sender import send_telegram_voice

                # 统一节流：与文本/照片共用墙钟，语音不与前一条外发瞬时双发。
                await self._presend_pace()
                sent = await send_telegram_voice(
                    self.client,
                    original_message.chat.id,
                    result.audio_path,
                    duration=dur_int,
                    reply_to_message_id=_rt,
                    opus_application=_opus_app,
                )
                # 发布到 /static **必须在 unlink 之前**（音频发完即删）；
                # 失败返回空 → 镜像退回纯文本，与改动前一致。
                _vmt, _vref = (self._publish_media_ref(result.audio_path)
                               if sent else ("", ""))
                try:
                    os.unlink(result.audio_path)
                except Exception:
                    pass

                if sent:
                    _note_voice_sent()
                    _note_voice_ok()
                    self.logger.info(
                        "[voice_reply] voice sent chat=%s persona=%s dur=%s",
                        original_message.chat.id,
                        voice_ctx.get("persona_id") or "",
                        dur_int,
                    )
                    daily_stats.bump("tts_sent")
                    # 连发监测（2026-07-15 三连发事故指纹回归防线）
                    from src.client.voice_burst_guard import note_voice_send
                    note_voice_send(original_message.chat.id, vr_cfg)
                    # 统一记账：语音也刷墙钟 + 计入今日外发量（反封号/健康灯不漏算语音条）。
                    self._postsend_record_count()
                    # 镜像正文＝干净念稿（[语音] 语义由 media_type 承载，与 B 线
                    # send_media(inbox_text=念稿) 同口径）；发布失败时 media_ref 为空，
                    # 前端按「无音频存档」转写行展示，media_type 仍如实标 voice。
                    # msg_id＝send_voice 返回的真实 id（回显主键去重，2026-08-02）。
                    _vclean = " ".join(str(reply_text or "").split())
                    _vmid = getattr(sent, "id", "") or ""
                    # P1-3：语音行带「谁的音色」（人设显示名，best-effort 空串安全）
                    from src.ai.persona_voice import persona_display_name
                    _vwho = persona_display_name(voice_ctx.get("persona_id"))
                    if vr_cfg.get("send_text_summary", False):
                        # 语音行本体也要可见/可回放（此前该分支只镜像文本摘要，语音
                        # 条在收件箱隐形）；contacts 由随后的 _send_reply 记一次，
                        # 这里只镜像不重复记账。
                        self._mirror_out_row(
                            original_message.chat.id, _vclean, msg_id=_vmid,
                            media_type=_vmt or "voice", media_ref=_vref,
                            sender_name=_vwho)
                        # 文本摘要走 _send_reply→自带护栏/节流/计数/镜像/记账
                        # （语音+文本=确有 2 条外发，各记一次属正确口径）。
                        await self._send_reply(original_message, reply_text)
                    else:
                        # 仅发语音时也要镜像/记账，否则坐席台/亲密度看不到这次外发。
                        # contacts 预览保留 [语音] 标记（纯文本时间线需要表意）。
                        self._mirror_out_row(
                            original_message.chat.id, _vclean, msg_id=_vmid,
                            media_type=_vmt or "voice", media_ref=_vref,
                            sender_name=_vwho)
                        self._record_contact_out(
                            original_message.chat.id,
                            self._voice_mirror_preview(reply_text))
                    return True
                _note_voice_fail("send_failed")
                return False

            # ── A1 text-first 编排（2026-07-15 阶段A）：合成超预算先发文字占位，
            # 语音后台补发；失败补发完整文字——彻底消灭「发语音后 3 分钟静默」。
            _tf = (vr_cfg.get("text_first")
                   if isinstance(vr_cfg.get("text_first"), dict) else {})
            _tf_budget = float(_tf.get("budget_sec", 25) or 25)
            if not bool(_tf.get("enabled", True)) or _tf_budget <= 0:
                return await _voice_flow()   # 旧行为：直等全流程

            async def _send_filler():
                # 2026-08-16 起默认关（老板裁定：宁可不发消息，也不发「嗯嗯我在，
                # 稍等哈～」类垫场句——8/16 22:23 生产实录被点名为兜底复读）。
                # 超预算改为静默等语音；语音最终失败仍补发完整真回复文字
                # （_send_fallback_text，那是真内容不受此开关影响）。
                # 显式配 text_first.filler: true 可恢复旧占位行为。
                if not bool(_tf.get("filler", False)):
                    return
                # 占位句复用 reply.ai_fallback_replies（现成的"在场感"话术池，
                # 措辞不硬承诺语音——语音失败改发文字也不算食言）；配置池空
                # 用内置池。per-chat 冷却 + 避开上一条措辞（2026-07-26 实锤：
                # 单句占位 4 分钟连发 3 次触发出站复读告警）。
                _pool = [str(x).strip() for x in
                         ((raw_cfg.get("reply") or {}).get("ai_fallback_replies")
                          or []) if str(x).strip()]
                _state = getattr(self, "_tf_filler_state", None)
                if _state is None:
                    _state = {}
                    self._tf_filler_state = _state
                _fkey = str(original_message.chat.id)
                _last_ts, _last_txt = _state.get(_fkey, (0.0, ""))
                _cd = float(_tf.get("filler_cooldown_sec", 180) or 0)
                _txt = pick_text_first_filler(
                    _pool, last_text=_last_txt, last_ts=_last_ts,
                    cooldown_sec=_cd)
                if _txt is None:
                    self.logger.info(
                        "[voice_reply] text-first：%.0fs 占位冷却窗内 → 本轮不发占位",
                        _cd)
                    return
                _state[_fkey] = (time.time(), _txt)
                if len(_state) > 2000:   # 防膨胀：清一天前的会话项
                    _cut = time.time() - 86400.0
                    for _k in [k for k, v in _state.items() if v[0] < _cut]:
                        _state.pop(_k, None)
                await self._send_reply(original_message, _txt)

            async def _send_fallback_text():
                # 无兜底纪律（2026-08-17 老板拍板，docs/实施33 v2）：语音失败**不再**
                # 自动改发文字——宁可不回消息，也不发替代品（旧「诚实回落改写+补发
                # 完整文字」路径整体拆除）。处置＝回复原文转工作台待发队列（内容不丢，
                # 修好后坐席一键放行）+ 主机弹窗「AI 不可用」+ ERROR 日志上报。
                _chat_id = str(getattr(
                    getattr(original_message, "chat", None), "id", "") or "")
                _acct = str(getattr(self, "account_id", "") or "default")
                _cid = ""
                _queued = False
                try:
                    from src.inbox.normalizer import conv_id as _conv_id
                    _cid = _conv_id("telegram", _acct, _chat_id)
                except Exception:
                    _cid = f"telegram:{_acct}:{_chat_id}"
                try:
                    from src.integrations.protocol_bridge import get_inbox_store
                    _ibx = get_inbox_store()
                    if _ibx is not None and str(reply_text or "").strip():
                        import time as _t
                        import uuid as _u
                        _ibx.upsert_draft({
                            "draft_id": f"voiceblock:{_u.uuid4().hex[:12]}",
                            "conversation_id": _cid,
                            "platform": "telegram",
                            "account_id": _acct,
                            "chat_key": _chat_id,
                            "source_kind": "voice_blocked",
                            "source_id": f"{_cid}:{int(_t.time())}",
                            "peer_text": str(
                                getattr(original_message, "text", None)
                                or getattr(original_message, "caption", None)
                                or "")[:500],
                            "draft_text": str(reply_text or ""),
                            "risk_level": "low",
                            "autopilot_level": "L1",
                            "status": "pending",
                            "created_at": _t.time(),
                        })
                        _queued = True
                except Exception:
                    self.logger.warning(
                        "[voice_reply] 语音失败回复转待发队列失败", exc_info=True)
                try:
                    from src.ops.delivery_block import report_block
                    report_block(
                        "voice",
                        reason=str((fail_state or {}).get("reason")
                                   or "synth_failed"),
                        platform="telegram",
                        conversation_id=_cid,
                        queued_draft=_queued)
                except Exception:
                    self.logger.debug(
                        "[voice_reply] delivery_block 上报失败", exc_info=True)

            import asyncio as _aio
            _vt = _aio.create_task(_voice_flow())
            return await self.race_voice_with_text_first(
                _vt, budget_sec=_tf_budget, send_filler=_send_filler,
                send_fallback_text=_send_fallback_text, logger=self.logger,
                already_sent=lambda: bool(_voice_progress["sent"]))
        except Exception as ex:
            self.logger.error("[voice_reply] unexpected error: %s", ex)
            return False

    # A1 text-first 后台看护任务引用（防 GC 取消；类级共享等价于进程级）
    _tf_bg_tasks: set = set()

    @staticmethod
    async def race_voice_with_text_first(
        voice_task, *, budget_sec: float, send_filler, send_fallback_text,
        logger, already_sent=None,
    ) -> bool:
        """A1「先文字后语音」编排（2026-07-15 阶段A）：3 分钟静默的解药。

        克隆合成在 3060 上要 30-110s，用户视角是「发了语音石沉大海」。本编排：
          - 预算内（budget_sec）语音完成 → 行为与旧链完全一致（含失败回落文字）；
          - 超预算 → 立刻发一条占位短文字稳住对话（"稍等哈~"，真人也这么干），
            语音继续后台合成：成了 → 补发语音（迟到的语音仍是惊喜）；
            败了 → 补发完整文字回复——对话在任何分支都绝不悬空。
        返回语义与 _maybe_send_voice_reply 对外一致：True=调用方不要再发文字
        （语音已发/本编排已接管文字兜底）；False=语音失败且未接管（调用方发文字）。
        """
        import asyncio as _aio
        try:
            done, _ = await _aio.wait({voice_task}, timeout=max(0.0, budget_sec))
        except Exception:
            done = {voice_task} if voice_task.done() else set()
        if done:
            try:
                return bool(voice_task.result())
            except Exception:
                logger.debug("[voice_reply] text-first 流程异常", exc_info=True)
                return False
        logger.info(
            "[voice_reply] text-first：合成超 %.0fs 预算 → 先发文字占位，语音后台继续",
            budget_sec)
        # 分条场景：预算到点时前几条可能已经送达（实锤：语音→占位文字→语音，
        # 占位反在语音之后到，语义彻底错乱）→ 已发过语音就不再补占位。
        try:
            if already_sent is not None and already_sent():
                logger.info(
                    "[voice_reply] text-first：已有语音条送达 → 跳过占位文字（防错序）")
                send_filler = None
        except Exception:
            logger.debug("[voice_reply] text-first 进度探测异常（忽略）", exc_info=True)
        if send_filler is not None:
            try:
                await send_filler()
            except Exception:
                logger.debug("[voice_reply] 占位文字发送失败（忽略）", exc_info=True)

        async def _watch():
            ok = False
            try:
                ok = bool(await voice_task)
            except Exception:
                ok = False
            if not ok:
                logger.info(
                    "[voice_reply] text-first：语音最终失败 → 交失败处置回调"
                    "（无兜底纪律：拦截+待发+弹窗，不自动改发文字）")
                try:
                    await send_fallback_text()
                except Exception:
                    logger.warning(
                        "[voice_reply] text-first 失败处置回调异常", exc_info=True)

        _t = _aio.create_task(_watch())
        TelegramSenderMixin._tf_bg_tasks.add(_t)
        _t.add_done_callback(TelegramSenderMixin._tf_bg_tasks.discard)
        return True

    async def _voice_recording_action(self, chat_id) -> None:
        """挂 Telegram「正在录制语音」状态（best-effort，约 5s 自动过期）。"""
        try:
            from pyrogram.enums import ChatAction
            await self.client.send_chat_action(chat_id, ChatAction.RECORD_AUDIO)
        except Exception:
            pass

    async def _voice_recording_gap(self, chat_id, gap_sec: float) -> None:
        """条间拟人间隔：睡满 ``gap_sec``，期间每 ~4s 续挂「录音中」状态。

        真人发第二条语音前要「按住录音」——对方看到的正是这个状态。状态挂失败
        不影响等待节奏（纯增强）。
        """
        remaining = max(0.0, float(gap_sec))
        while remaining > 0:
            await self._voice_recording_action(chat_id)
            step = min(4.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _send_voice_reply_parts(
        self, original_message, parts, tts, voice_ctx, vr_cfg, split_cfg,
        *, timeout_sec: float, opus_application: str = "voip",
        pre_colloquialized: bool = False,
        skip_llm_colloquial: bool = False,
        on_part_sent=None,
    ) -> bool:
        """分条语音发送（活人感核心）：先全部合成，再按真人录音节奏逐条发。

        设计（拟人化 > 延迟，运营方针）：
          - **先合成后表演**：全部条目合成成功才开始发送——节奏由我们编排
            （下一条的间隔 ≈ 下一条音频时长 ×gap_factor + 思考抖动），而不是
            被 GPU 合成进度牵着走；任何一条失败 → 返回 False 回落整段路径，
            绝不出现「发了一半没下文」。
          - 合成期间与条间间隔都挂「正在录音」chat action（对方视角=真人在录）。
          - 每条独立走预渲染/缓存命中（短句命中率更高）；只有第一条 reply 引用
            原消息（真人连发也只有第一条是"回复"）。
          - 记账：每条各记一次外发（反封号口径）；**镜像逐条**（2026-08-02）——
            客户实际收到 N 条独立语音，收件箱就有 N 行（每行自己的 msg_id/念稿/
            可回放音频）。旧口径「合并一行 [语音]×N 不附音频」正是「坐席看不到
            自己发的语音」的最后一块缺口。contacts 亲密度仍按一轮记一次。
        返回 True=至少发出一条（调用方不再发文字）。
        """
        import random as _rnd

        from src.ai.tts_quality import looks_truncated, resolve_quality_gate

        chat_id = original_message.chat.id
        max_sec = float(vr_cfg.get("max_seconds", 60) or 60)
        emotion = voice_ctx.get("emotion")
        _qg = resolve_quality_gate(vr_cfg)

        # 合成期间挂「录音中」（fire-and-forget 一次即可，5s 会过期；
        # 合成耗时由 GPU 决定，对方看到断续的录音状态反而真实）
        await self._voice_recording_action(chat_id)

        results = []
        for i, p in enumerate(parts):
            # 分条活人感：只首条允许口语化「句首迟疑词」（其实，/话说，），
            # 后续条 colloquial_lead=False——连发 2-3 条都同样开头会做作。
            # pre_colloquialized=True（生成层口语版）时整段已是口语，各条跳过改写。
            rv = await tts.synthesize(
                p, timeout_sec=timeout_sec, emotion=emotion,
                # 整段已 LLM 口语化过（含句首起头）→ 各条都不再加 lead，
                # 免得首条被规则档二次起头（「其实，说真的，…」）。
                colloquial_lead=(i == 0 and not skip_llm_colloquial),
                pre_colloquialized=pre_colloquialized,
                skip_llm_colloquial=skip_llm_colloquial,
                # 分条单条：hub_fish 按 best_of_parts 减候选（GPU 减负）
                split_part=True)
            if not rv.ok:
                self.logger.info(
                    "[voice_reply] 分条第 %d/%d 条合成失败(%s)",
                    len(results) + 1, len(parts), rv.error)
                for r in results:
                    try:
                        os.unlink(r.audio_path)
                    except Exception:
                        pass
                return False
            try:
                from src.inbox.voice_autosend import (
                    resolve_no_edge_fallback,
                    should_reject_voice_tts_result,
                )
                _raw_cfg = self.config.config if hasattr(self.config, "config") else {}
                _no_edge = resolve_no_edge_fallback(
                    _raw_cfg, (_raw_cfg.get("telegram") or {}).get("voice_reply") or {})
                if should_reject_voice_tts_result(rv, no_edge=_no_edge):
                    self.logger.warning(
                        "[voice_reply] 分条 no_edge 拒发 edge → 回落整段")
                    for r in results:
                        try:
                            os.unlink(r.audio_path)
                        except Exception:
                            pass
                    try:
                        os.unlink(rv.audio_path)
                    except Exception:
                        pass
                    return False
            except Exception:
                pass
            # 质量闸门（2026-07-15「乱码语音」防线）：单条时长低于该文本的
            # 物理最快语速 → 判截断/坏音，整批放弃回落（绝不把半截杂音发出去）。
            if _qg["enabled"]:
                _bad, _why = looks_truncated(
                    p, rv.duration_sec,
                    min_sec_per_unit=_qg["min_sec_per_unit"],
                    min_units=_qg["min_units"])
                if _bad:
                    self.logger.warning(
                        "[voice_reply] 分条第 %d/%d 条疑似截断(%s) → 回落整段",
                        len(results) + 1, len(parts), _why)
                    try:
                        from src.ai.avatar_voice_stats import get_avatar_voice_stats
                        get_avatar_voice_stats().record_truncation_reject()
                    except Exception:
                        pass
                    for r in results:
                        try:
                            os.unlink(r.audio_path)
                        except Exception:
                            pass
                    try:
                        os.unlink(rv.audio_path)
                    except Exception:
                        pass
                    return False
            results.append(rv)

        total_dur = sum(r.duration_sec for r in results if r.duration_sec > 0)
        if total_dur > max_sec * 1.5:
            self.logger.warning(
                "[voice_reply] 分条总时长 %.1fs 超限(%.1fs) → 回落", total_dur,
                max_sec * 1.5)
            for r in results:
                try:
                    os.unlink(r.audio_path)
                except Exception:
                    pass
            return False

        from src.client.voice_sender import send_telegram_voice

        _rt = self._reply_to_message_id_for_send(original_message)
        gap_factor = float(split_cfg.get("gap_factor", 1.1) or 1.1)
        jit = split_cfg.get("gap_jitter_sec") or [1.0, 2.5]
        try:
            jit_lo, jit_hi = float(jit[0]), float(jit[1])
        except Exception:
            jit_lo, jit_hi = 1.0, 2.5
        max_gap = float(split_cfg.get("max_gap_sec", 20) or 20)
        # P1-3：语音人设显示名（「谁的音色」徽标）——循环外解析一次，空串安全。
        from src.ai.persona_voice import persona_display_name as _pdn
        _v_sender = _pdn(voice_ctx.get("persona_id"))

        sent_n = 0
        for i, rv in enumerate(results):
            if i > 0:
                # 间隔 ≈ 「按住录音」下一条所需时间 + 思考抖动（真随机——发送
                # 节奏无缓存语义，自然抖动比确定性更拟人）
                base = rv.duration_sec if rv.duration_sec > 0 else 3.0
                gap = min(max_gap, base * gap_factor + _rnd.uniform(jit_lo, jit_hi))
                await self._voice_recording_gap(chat_id, gap)
            await self._presend_pace()
            dur_int = int(rv.duration_sec) if rv.duration_sec > 0 else None
            sent_msg = await send_telegram_voice(
                self.client, chat_id, rv.audio_path, duration=dur_int,
                reply_to_message_id=(_rt if i == 0 else None),
                opus_application=opus_application)
            # 发布到 /static **必须在 unlink 之前**（音频发完即删）；失败回空 →
            # 该条镜像成「无音频存档」转写行（media_type 仍如实标 voice）。
            _pmt, _pref = (self._publish_media_ref(rv.audio_path)
                           if sent_msg else ("", ""))
            try:
                os.unlink(rv.audio_path)
            except Exception:
                pass
            if not sent_msg:
                self.logger.warning(
                    "[voice_reply] 分条第 %d/%d 条发送失败，停止后续",
                    i + 1, len(results))
                for r in results[i + 1:]:
                    try:
                        os.unlink(r.audio_path)
                    except Exception:
                        pass
                break
            sent_n += 1
            # 告知 text-first 编排「已有语音条送达」→ 不再补占位文字（防错序）
            if on_part_sent is not None:
                try:
                    on_part_sent()
                except Exception:
                    pass
            # 连发监测（2026-07-15 三连发事故指纹回归防线）：分条每条都记账
            from src.client.voice_burst_guard import note_voice_send
            note_voice_send(chat_id, vr_cfg)
            self._postsend_record_count()
            # 逐条镜像（2026-08-02）：每条自己的 msg_id（回显主键去重）/该条念稿/
            # 该条音频——坐席可逐条回放。contacts 记账在循环外按一轮记一次。
            self._mirror_out_row(
                chat_id, " ".join(str(list(parts)[i]).split()),
                msg_id=getattr(sent_msg, "id", "") or "",
                media_type=_pmt or "voice", media_ref=_pref,
                sender_name=_v_sender)

        if sent_n:
            self.logger.info(
                "[voice_reply] 分条语音已发 %d/%d 条 chat=%s persona=%s 总时长=%.1fs",
                sent_n, len(results), chat_id,
                voice_ctx.get("persona_id") or "", total_dur)
            daily_stats.bump("tts_sent")
            # contacts 外发互动：一轮分条记一次；预览带 ×N 与**已发出那几条**的
            # 念稿（后续条失败时不带——contacts 时间线不能谎报送达量）。
            self._record_contact_out(
                chat_id,
                self._voice_mirror_preview(
                    " ".join(str(p) for p in list(parts)[:sent_n]), sent_n))
        return sent_n > 0

    async def _forward_escalation_user_to_agents(self, spec) -> None:
        """
        人工转接触发且群内回复已发出后：把用户在该群的原消息转发到各客服私聊，
        并可选再发一条带内联按钮 + 直达链接的说明（forward_private_jump_hint，默认开）。
        spec: from_chat_id, message_id, targets, chat_username?, chat_title?
        """
        if not spec or not self.client:
            return
        from_chat_id = spec.get("from_chat_id")
        mid = spec.get("message_id")
        targets = spec.get("targets") or []
        if from_chat_id is None or mid is None:
            return
        try:
            mid_int = int(mid)
        except (TypeError, ValueError):
            return
        if mid_int <= 0:
            return
        for t in targets:
            uid = int(t.get("user_id") or 0)
            un = (t.get("username") or "").strip().lstrip("@")
            peer = uid if uid > 0 else (un or None)
            if peer is None:
                continue
            forward_ok = False
            try:
                await self.client.forward_messages(
                    chat_id=peer,
                    from_chat_id=from_chat_id,
                    message_ids=mid_int,
                )
                forward_ok = True
                self.logger.info(
                    "人工转接: 已转发用户原消息 → 客服 peer=%s from_chat=%s msg_id=%s",
                    peer,
                    from_chat_id,
                    mid_int,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 转发至客服 peer=%s 失败: %s", peer, e
                )
            try:
                await self._send_escalation_private_jump_hint(
                    peer,
                    spec,
                    mid_int,
                    after_forward_ok=forward_ok,
                )
            except Exception as ex:
                self.logger.warning(
                    "人工转接: 私聊定位跟进异常 peer=%s: %s", peer, ex
                )

        group_target = spec.get("group_target")
        if isinstance(group_target, dict):
            group_id = (group_target.get("group_id") or "").strip()
            if group_id:
                try:
                    group_peer = int(group_id) if group_id.lstrip("-").isdigit() else group_id
                    await self.client.forward_messages(
                        chat_id=group_peer,
                        from_chat_id=from_chat_id,
                        message_ids=mid_int,
                    )
                    self.logger.info(
                        "人工转接: 已转发用户原消息 → 客服群 group=%s msg_id=%s",
                        group_id, mid_int,
                    )
                except Exception as e:
                    self.logger.warning(
                        "人工转接: 转发至客服群 group=%s 失败: %s", group_id, e
                    )
