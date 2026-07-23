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

    def _presend_blocked(self, *, is_autoreply: bool = False) -> bool:
        """发送前统一护栏：G1 全局 Kill-Switch + N 线反封号闸门 + 账号限速/熔断 + 营业时段。

        返回 True=应跳过本次外发（冻结/被闸门拦）；任何异常一律静默放行（绝不因护栏自身报错阻断发送）。
        文本回复与形象照直发共用本判断——避免「文字被拦但图照发」的风控绕过。

        ``is_autoreply``：True=本次是「入站自动回复」（受营业时段 hours 约束——非营业时段
        转人工不自动发）；False=主动发/坐席接管/编排器/测试（只受限速与急停，不被时段拦，
        坐席深夜也能联系客户）。限速（时/日上限+熔断）对两类外发一律生效（账号级安全）。
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
            from src.skills.companion_send_gate import evaluate, gate_enabled
            from src.skills.account_signals import build_account_signals
            if gate_enabled(_gcfg):
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
                    self.logger.warning(
                        "[send_gate] 账号 %s 被反封号闸门拦截: %s (light=%s, score=%s)",
                        _sig["account_id"], _dec.get("reason"),
                        _dec.get("light"), _dec.get("score"),
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

    async def run_prereply_humanize(
        self, chat_id, *, text: str = "", elapsed_sec: float = 0.0,
    ) -> None:
        """原生 A 线文本回复前的拟人序列：已读 → 正在输入(续挂) → 思考延迟。

        与全自动 autosend 共用 ``humanize`` 协作器与 ``compute_pacing_delay`` 装配，节奏一致。
        延迟取 ``telegram.reply_humanize.thinking_delay``；**默认 0=不延迟**（只已读、不改
        现有近即时手感）。配了 min/max 后：``adaptive=false`` 走 uniform(min,max)；
        ``adaptive=true`` 按回复 ``text`` 长度/``emotion`` 自适应估时并扣除 ``elapsed_sec``
        已耗时（入站至今）。语音回复不走此路（自带「正在录音」分条节奏）。best-effort。
        """
        if chat_id is None:
            return
        try:
            raw_cfg = self.config.config if hasattr(self.config, "config") else {}
            rh = (raw_cfg.get("telegram") or {}).get("reply_humanize") or {}
            from src.inbox.humanize import resolve_pacing, run_presend_humanization
            # 人设化节奏 + 观测分维：账号人设（与语音路径同口径 account_persona_ids[0]）。
            _pid = ""
            try:
                _pids = getattr(self, "account_persona_ids", None)
                _pid = str(_pids[0]) if _pids else ""
            except Exception:
                _pid = ""
            # arousal 由 resolve_pacing 从回复 text 自动估（语义正确：回复自身激活度）。
            _pr = resolve_pacing(
                rh.get("thinking_delay") or {}, text=text, elapsed_sec=elapsed_sec,
                persona_id=_pid)
            try:
                from src.integrations.humanize_metrics import record_pacing
                record_pacing(f"native_tg/{_pid or '-'}", _pr)
            except Exception:
                pass
            delay = _pr.delay

            async def _mr():
                await self._mark_peer_read(chat_id)

            async def _tp(_action):
                await self._send_typing_action(chat_id)

            await run_presend_humanization(
                delay=delay, action="typing",
                mark_read=_mr, typing=_tp, sleep=asyncio.sleep)
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
        """
        self._last_send_wallclock = time.time()
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
                                    msg_id: Any = "") -> None:
        """发送成功后：出站镜像到坐席台（N4b）+ 记入 contacts 的外发互动（Q3）。

        文本回复与富媒体（照片/语音）共用——富媒体传带标记的 preview（如「[图片] 配文」/「[语音]」），
        让坐席台**看见** AI 发了富媒体、IntimacyEngine 也**计入**一次外发（否则只见入站、mutuality 偏低）。
        两步各自 best-effort，绝不阻断发送。

        ``msg_id``：发送 API 返回的真实 message.id（治本幂等键）。带上它后乐观出站镜像行与
        「自身已发消息被回显」共用同一 platform_msg_id → 主键级精确去重，不再依赖时间窗近似。
        """
        try:
            _emit = getattr(self, "_emit_inbox", None)
            if _emit is not None:
                _emit(chat_id=chat_id, text=preview, direction="out",
                      msg_id=str(msg_id or ""))
                # P4-4：镜像出站即置「已发送」（单勾）；对端读后由 UpdateReadHistoryOutbox
                # 回执升级为「已读」（蓝色双勾）。仅 companion 镜像开启且带真实 id 时生效。
                if getattr(self, "_mirror_inbox", False) and msg_id:
                    from src.integrations.protocol_bridge import report_message_status
                    report_message_status(
                        "telegram", getattr(self, "account_id", "default"),
                        str(chat_id), str(msg_id), "sent")
        except Exception:
            pass
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
        best-effort：记账失败绝不影响发送主流程。
        """
        try:
            from src.client.reply_logic_gates import effective_streak
            ts_map = getattr(self, '_auto_reply_ts', None)
            streak_map = getattr(self, '_auto_reply_streak', None)
            if ts_map is None or streak_map is None:
                return
            key = f"{chat_id}:{user_id}"
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
            if self._presend_blocked(is_autoreply=True):
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
            self.logger.info("已回复消息: %s", self._log_safe_text(reply_text))
        except Exception as e:
            self.logger.error("发送回复失败: %s", e)
            self._handle_send_exc(e)   # G2 分级急停 + 实施31 TG 告警（best-effort）

    async def _send_text_guarded(self, chat_id: int, text: str):
        """A 线外发文本核心：过发送前护栏 + 节流 + 记账，返回 ``(ok, sent_message)``。

        - ``ok``：是否成功送出（过护栏且未抛；与旧 ``send_message`` 的 bool 语义一致）。
        - ``sent_message``：底层 ``client.send_message`` 的返回（真实 pyrogram 为 ``Message``，
          可取 ``.id``；测试桩/无返回时为 None）。

        **不**做出站镜像（避免与编排器中心化收件箱回写重复镜像）。
        """
        try:
            # 统一发送前护栏：G1 Kill-Switch + N 线反封号闸门（与 _send_reply/send_photo 共用）
            if self._presend_blocked():
                return False, None
            await self._presend_pace()
            if not self.client:
                self.logger.error("客户端未初始化")
                return False, None
            _sent = await self.client.send_message(chat_id, text)
            self._postsend_record_count()
            self.logger.info("已发送消息到 %s: %s...", chat_id, text[:50])
            return True, _sent
        except Exception as e:
            self.logger.error("发送消息失败: %s", e)
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
            if self._presend_blocked():
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
            # 出站镜像 + contacts 记账：坐席台看见「AI 发了图」、亲密度计入这次外发。
            _cap = (caption or "").strip()
            self._postsend_mirror_and_record(
                chat_id, f"[图片] {_cap}".strip() if _cap else "[图片]",
                msg_id=getattr(_sent, "id", "") or "")
            self.logger.info("已发送照片到 %s（%s）", chat_id, photo_path)
            return True
        except Exception as e:
            self.logger.error("发送照片失败: %s", e)
            self._handle_send_exc(e)   # G2 分级急停 + 实施31 TG 告警（best-effort）
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
    ) -> bool:
        """Try to send a TTS voice note for *reply_text*.

        Returns ``True`` if a voice note was sent (caller should skip text send).
        Returns ``False`` if voice was skipped/failed (caller sends text normally).

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

            trigger = str(vr_cfg.get("trigger", "when_peer_voice")).strip().lower()
            if trigger == "never":
                self.logger.debug("[voice_reply] skip: trigger=never")
                return False
            if trigger == "when_peer_voice" and not is_peer_voice:
                self.logger.debug("[voice_reply] skip: trigger=when_peer_voice but msg is not voice")
                return False
            if trigger == "random":
                prob = float(vr_cfg.get("probability", 0.3) or 0.3)
                if random.random() >= prob:
                    return False
            if trigger == "smart":
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
            clean_text = (reply_text or "").strip()
            if not clean_text or len(clean_text) > max_chars:
                self.logger.debug(
                    "[voice_reply] skipped: text len=%d max=%d", len(clean_text), max_chars
                )
                return False

            # 统一发送前护栏（与文本/照片共用）：冻结/被反封号闸门拦 → 不出语音、也不白跑 TTS。
            # 返回 False → 调用方回退文本 _send_reply，文本同样会被护栏拦 → 冻结期彻底静默。
            if self._presend_blocked():
                self.logger.info("[voice_reply] skip: 发送前护栏拦截（kill-switch/反封号闸门）")
                return False

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
                _spoken = take_spoken_variant(reply_text)
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

            async def _voice_flow() -> bool:
                """合成→质检→发送全流程（A1 抽为内协程：可被 text-first 预算编排）。"""
                # ── 分条发送（活人感）：长回复像真人一样连发 2-3 条短语音，条间按
                # 「按住录音的时长」留拟人间隔 + 挂"录音中"状态。全部合成成功才发
                # （节奏是表演出来的，不被 GPU 进度驱动）；任何失败回落单条整段路径。
                split_cfg = (vr_cfg.get("split_send")
                             if isinstance(vr_cfg.get("split_send"), dict) else {})
                if (split_cfg.get("enabled", False)
                        and len(synth_source) >= int(split_cfg.get("min_total_chars", 24) or 24)):
                    from src.ai.voice_clone_client import pack_voice_parts
                    parts = pack_voice_parts(
                        synth_source,
                        part_max_chars=int(split_cfg.get("part_max_chars", 40) or 40),
                        max_parts=int(split_cfg.get("max_parts", 3) or 3),
                        min_tail_chars=int(split_cfg.get("min_tail_chars", 8) or 0))
                    if len(parts) >= 2:
                        split_sent = await self._send_voice_reply_parts(
                            original_message, parts, tts, voice_ctx, vr_cfg, split_cfg,
                            timeout_sec=timeout_sec, opus_application=_opus_app,
                            pre_colloquialized=bool(_spoken))
                        if split_sent:
                            if vr_cfg.get("send_text_summary", False):
                                await self._send_reply(original_message, reply_text)
                            return True
                        self.logger.info("[voice_reply] 分条路径未完成 → 回落整段单条")

                result = await tts.synthesize(
                    synth_source, timeout_sec=timeout_sec,
                    emotion=voice_ctx.get("emotion"),
                    pre_colloquialized=bool(_spoken))
                if not result.ok:
                    self.logger.warning("[voice_reply] TTS failed: %s", result.error)
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
                try:
                    os.unlink(result.audio_path)
                except Exception:
                    pass

                if sent:
                    self.logger.info(
                        "[voice_reply] voice sent chat=%s persona=%s dur=%s",
                        original_message.chat.id,
                        voice_ctx.get("persona_id") or "",
                        dur_int,
                    )
                    # 连发监测（2026-07-15 三连发事故指纹回归防线）
                    from src.client.voice_burst_guard import note_voice_send
                    note_voice_send(original_message.chat.id, vr_cfg)
                    # 统一记账：语音也刷墙钟 + 计入今日外发量（反封号/健康灯不漏算语音条）。
                    self._postsend_record_count()
                    if vr_cfg.get("send_text_summary", False):
                        # 文本摘要走 _send_reply→自带护栏/节流/计数/镜像/记账
                        # （语音+文本=确有 2 条外发，各记一次属正确口径）。
                        await self._send_reply(original_message, reply_text)
                    else:
                        # 仅发语音时也要镜像/记账，否则坐席台/亲密度看不到这次外发。
                        self._postsend_mirror_and_record(
                            original_message.chat.id, "[语音]")
                    return True
                return False

            # ── A1 text-first 编排（2026-07-15 阶段A）：合成超预算先发文字占位，
            # 语音后台补发；失败补发完整文字——彻底消灭「发语音后 3 分钟静默」。
            _tf = (vr_cfg.get("text_first")
                   if isinstance(vr_cfg.get("text_first"), dict) else {})
            _tf_budget = float(_tf.get("budget_sec", 25) or 25)
            if not bool(_tf.get("enabled", True)) or _tf_budget <= 0:
                return await _voice_flow()   # 旧行为：直等全流程

            async def _send_filler():
                if not bool(_tf.get("filler", True)):
                    return
                # 占位句复用 reply.ai_fallback_replies（现成的"在场感"话术池，
                # 措辞不硬承诺语音——语音失败改发文字也不算食言）
                _pool = [str(x).strip() for x in
                         ((raw_cfg.get("reply") or {}).get("ai_fallback_replies")
                          or []) if str(x).strip()]
                await self._send_reply(
                    original_message,
                    random.choice(_pool) if _pool else "稍等我一下哈～")

            async def _send_fallback_text():
                await self._send_reply(original_message, reply_text)

            import asyncio as _aio
            _vt = _aio.create_task(_voice_flow())
            return await self.race_voice_with_text_first(
                _vt, budget_sec=_tf_budget, send_filler=_send_filler,
                send_fallback_text=_send_fallback_text, logger=self.logger)
        except Exception as ex:
            self.logger.error("[voice_reply] unexpected error: %s", ex)
            return False

    # A1 text-first 后台看护任务引用（防 GC 取消；类级共享等价于进程级）
    _tf_bg_tasks: set = set()

    @staticmethod
    async def race_voice_with_text_first(
        voice_task, *, budget_sec: float, send_filler, send_fallback_text,
        logger,
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
                logger.info("[voice_reply] text-first：语音最终失败 → 补发完整文字")
                try:
                    await send_fallback_text()
                except Exception:
                    logger.warning(
                        "[voice_reply] text-first 文字兜底发送失败", exc_info=True)

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
          - 记账：每条各记一次外发（反封号口径）；镜像一次 ``[语音]×N``。
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
                colloquial_lead=(i == 0),
                pre_colloquialized=pre_colloquialized)
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
            ok = await send_telegram_voice(
                self.client, chat_id, rv.audio_path, duration=dur_int,
                reply_to_message_id=(_rt if i == 0 else None),
                opus_application=opus_application)
            try:
                os.unlink(rv.audio_path)
            except Exception:
                pass
            if not ok:
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
            # 连发监测（2026-07-15 三连发事故指纹回归防线）：分条每条都记账
            from src.client.voice_burst_guard import note_voice_send
            note_voice_send(chat_id, vr_cfg)
            self._postsend_record_count()

        if sent_n:
            self.logger.info(
                "[voice_reply] 分条语音已发 %d/%d 条 chat=%s persona=%s 总时长=%.1fs",
                sent_n, len(results), chat_id,
                voice_ctx.get("persona_id") or "", total_dur)
            self._postsend_mirror_and_record(
                chat_id, f"[语音]×{sent_n}" if sent_n > 1 else "[语音]")
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
