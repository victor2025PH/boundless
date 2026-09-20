"""
触发决策 Mixin：判断是否回复群组消息
包含回复链、追问上下文、会话窗口、AI上下文、L2兜底、四层/旧版触发
"""

import asyncio
import re
import time
from typing import Optional, Tuple, Dict, Any


class TelegramTriggerMixin:

    def _should_reply_by_reply_chain(self, message) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        reply_chain = reply_logic.get('reply_chain', {})
        if not reply_chain.get('enabled', True):
            return False
        if not reply_chain.get('reply_to_me_always_reply', True):
            return False
        reply_to = getattr(message, 'reply_to_message', None)
        if not reply_to:
            return False
        from_user = getattr(reply_to, 'from_user', None)
        if not from_user:
            return False
        my_id = getattr(self.user_info, 'id', None) if self.user_info else None
        if my_id is None:
            return False
        if getattr(from_user, 'id', None) != my_id:
            return False
        skip_short = reply_chain.get('skip_if_only_emoji_or_short', False)
        if skip_short:
            text = (message.text or message.caption or "").strip()
            if len(text) <= 2 and not any(c in text for c in "?？吗呢"):
                return False
        self.logger.info("[回复链] 用户回复了我们的消息，判定应回复")
        return True

    async def _should_reply_by_follow_up_context(self, message, current_text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        follow_up = reply_logic.get('follow_up', {})
        if not follow_up.get('enabled', True):
            return False
        if not self._group_bare_gate(message):
            return False
        if not self.client or not self.user_info:
            return False
        lookback = max(5, min(20, follow_up.get('lookback_count', 10)))
        max_len = follow_up.get('max_text_length', 200)
        try:
            chat_id = message.chat.id
            my_id = getattr(self.user_info, 'id', None)
            if my_id is None:
                return False
            t = (current_text or "").strip()
            if not t or len(t) > max_len:
                return False
            found_our_message = False
            count = 0
            async for msg in self.client.get_chat_history(chat_id, limit=lookback):
                count += 1
                if count == 1:
                    continue
                from_user = getattr(msg, 'from_user', None)
                if not from_user:
                    continue
                if getattr(from_user, 'id', None) == my_id:
                    found_our_message = True
                    break
            if not found_our_message:
                return False
            if t in ("1", "2", "3", "4", "5") or (len(t) <= 3 and t.rstrip(".。,，、").strip() in ("1", "2", "3", "4", "5")):
                self.logger.info("[追问上下文] 最近 %d 条内曾出现我们且当前为选项数字 1~5，应回复", lookback)
                return True
            question_marks = "?？吗呢啊呀"
            follow_up_keywords = (
                "什么时候", "多久", "怎么", "怎样", "如何", "为什么", "能否", "可以吗",
                "when", "how", "what", "why", "order", "inquiry", "status", "check",
                "回调", "可用", "到账", "出来", "好了吗", "查到了吗",
                "pedido", "consulta", "pagamento", "sipariş", "sorgu", "durum",
                "طلب", "استفسار", "حالة", "commande", "statut", "bestellung", "anfrage",
                "ordine", "richiesta", "заказ", "запрос", "статус", "pago", "estado",
            )
            t_lower = t.lower()
            if any(c in question_marks for c in t) or any(k in t_lower for k in follow_up_keywords):
                self.logger.info("[追问上下文] 最近 %d 条内曾出现我们且当前像追问，应回复", lookback)
                return True
            return False
        except Exception as e:
            self.logger.debug("追问上下文检查异常: %s", e)
            return False

    def _group_bare_gate(self, message) -> bool:
        """群聊兜底闸（P3-2）：无显式信号路径在群里只对「刚互动过的人」放行。

        follow_up / l2_fallback / ai_context 三条兜底原是私聊语义——群里
        「45 分钟窗 + 10 条内我说过话」= 见谁都抢答（2026-07-25 测试群实锤：
        用户点名「每句都回复」）。收紧为：群聊中仅当「我们最近回复过这位
        发言者」且在群短窗（``telegram.group_reply.follow_window_minutes``，
        默认 3 分钟，0=群里彻底关兜底）内才放行；reply/@/关键词/L1 显式
        信号不经此闸。私聊恒放行（行为一字不变）。
        """
        from src.client.reply_logic_gates import normalize_chat_type
        ctype = normalize_chat_type(
            getattr(getattr(message, 'chat', None), 'type', ''))
        if ctype not in ('group', 'supergroup', 'channel'):
            return True
        from_user = getattr(message, 'from_user', None)
        uid = getattr(from_user, 'id', None) if from_user else None
        cid = getattr(getattr(message, 'chat', None), 'id', None)
        if uid is None or cid is None:
            return False
        grp_cfg = self.config.get('telegram', {}).get('group_reply', {})
        try:
            win_min = float(grp_cfg.get('follow_window_minutes', 3))
        except (TypeError, ValueError):
            win_min = 3.0
        if win_min <= 0:
            return False
        ts = self._session_reply_ts.get(f"{cid}:{uid}")
        return ts is not None and (time.time() - ts) <= win_min * 60

    def _record_session_reply(self, chat_id: int, user_id: int) -> None:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        if not reply_logic.get('session_window', {}).get('enabled', True):
            return
        key = f"{chat_id}:{user_id}"
        self._session_reply_ts[key] = time.time()
        window_min = reply_logic.get('session_window', {}).get('reply_within_minutes', 45)
        expire = time.time() - (window_min * 2 * 60)
        to_del = [k for k, ts in self._session_reply_ts.items() if ts < expire]
        for k in to_del:
            del self._session_reply_ts[k]

    def _is_in_session_window(self, chat_id: int, user_id: int) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        sw = reply_logic.get('session_window', {})
        if not sw.get('enabled', True):
            return False
        window_min = sw.get('reply_within_minutes', 45)
        key = f"{chat_id}:{user_id}"
        ts = self._session_reply_ts.get(key)
        if ts is None:
            return False
        if time.time() - ts > window_min * 60:
            del self._session_reply_ts[key]
            return False
        return True

    async def _get_previous_message(self, chat_id: int) -> Optional[Tuple[str, str]]:
        if not self.client:
            return None
        try:
            count = 0
            async for msg in self.client.get_chat_history(chat_id, limit=3):
                count += 1
                if count == 2:
                    prev_text = (msg.text or msg.caption or "").strip() or "[无文字]"
                    prev_date = getattr(msg, "date", None)
                    if prev_date:
                        try:
                            ts = prev_date.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            ts = str(prev_date)
                    else:
                        ts = ""
                    return (ts, prev_text[:800])
            return None
        except Exception as e:
            self.logger.debug("获取前一条消息异常: %s", e)
            return None

    async def _should_reply_by_ai_context(self, message, text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        cfg = reply_logic.get('ai_context_reply', {})
        if not cfg.get('enabled', True):
            return False
        if not self._group_bare_gate(message):
            return False
        if not self.ai_client or not text or len(text) > 600:
            return False
        prev = await self._get_previous_message(message.chat.id)
        if not prev:
            return False
        prev_time, prev_text = prev
        timeout = float(cfg.get('timeout_seconds', 8))
        try:
            should, reason = await asyncio.wait_for(
                self.ai_client.should_reply_by_context(prev_text, prev_time, text),
                timeout=timeout,
            )
            if should:
                self.logger.info("[AI上下文] 判定与工作相关应回复: %s", reason[:80] if reason else "")
                return True
            return False
        except asyncio.TimeoutError:
            self.logger.debug("AI上下文判断超时")
            return False
        except Exception as e:
            self.logger.debug("AI上下文判断异常: %s", e)
            return False

    async def _should_reply_by_l2_fallback(self, message, text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        l2_cfg = reply_logic.get('l2_fallback', {})
        if not l2_cfg.get('enabled', True):
            return False
        if not self._group_bare_gate(message):
            return False
        if not self.four_layer_trigger:
            return False
        if not message.from_user:
            return False
        chat_id = message.chat.id
        user_id = message.from_user.id
        if l2_cfg.get('only_in_session_window', True) and not self._is_in_session_window(chat_id, user_id):
            return False
        min_len = l2_cfg.get('text_min_len', 2)
        max_len = l2_cfg.get('text_max_len', 300)
        if len(text) < min_len or len(text) > max_len:
            return False
        timeout = l2_cfg.get('timeout_seconds', 6)
        try:
            chat_id_str = f"group_{chat_id}"
            should, reason = await asyncio.wait_for(
                self.four_layer_trigger.check_l2_only(text, chat_id_str, user_id),
                timeout=float(timeout)
            )
            if should:
                self.logger.info("[L2兜底] 会话窗口内 L2 判定应回复: %s", reason[:80] if reason else "")
                return True
            return False
        except asyncio.TimeoutError:
            self.logger.debug("L2兜底超时，不回复")
            return False
        except Exception as e:
            self.logger.debug("L2兜底异常: %s", e)
            return False

    def _contains_mention_of_self(self, message) -> bool:
        if not self.user_info:
            return False
        text = (message.text or message.caption or "")
        if not text:
            return False
        my_username = getattr(self.user_info, 'username', None)
        if not my_username:
            return False
        return my_username.lower() in text.lower() or f"@{my_username}".lower() in text.lower()

    async def _bug_intake_archive_capped_photo(self, message) -> None:
        """B33：报障群被压制的带图消息，图仍归档（protocol_media + bug_events）。

        只下载登记、不回复不引燃；任何失败只打 debug 日志，绝不影响主链。
        """
        try:
            from src.integrations.protocol_bridge import download_tg_media
            from src.ops.bug_intake import record_capped_photo
            _, url = await download_tg_media(
                message, getattr(self, "account_id", "default"))
            if url:
                self.logger.info("[bug_intake] 压制消息截图已归档: %s", url)
                record_capped_photo(
                    chat_id=getattr(getattr(message, "chat", None), "id", ""),
                    sender_id=getattr(getattr(message, "from_user", None), "id", ""),
                    media_url=url)
        except Exception:
            self.logger.debug("[bug_intake] 压制截图归档失败（忽略）", exc_info=True)

    async def _should_reply_to_group_message(self, message) -> bool:
        # 报障群值守三态（bug_intake，2026-08-18；2026-08-20 收口串戏）：
        # None=非报障群，走下面原有触发链一字不变；True=报障/用法/收集窗/点名，
        # 引燃；False=限频/危机词/闲聊/纯图，硬压制——报障群内 bug_intake 是唯一
        # 触发裁决者（闲聊落回 follow_window 会让支持号用陪伴人设接话，实录：
        # 官方支持号在报障群聊「Burrata 意面」）。is_direct=@提及/回复本账号。
        try:
            from src.ops.bug_intake import trigger_verdict
            _bi_cfg = (self.config.config
                       if hasattr(self.config, "config") else self.config)
            _bi_direct = False
            try:
                _bi_direct = bool(self._contains_mention_of_self(message)) or bool(
                    self.user_info and self._should_reply_by_reply_chain(message))
            except Exception:
                _bi_direct = False
            # 引用文本并入触发判定（2026-08-20：引用一条报障消息+「分析」两个字，
            # 裸正文判闲聊会被静默——引用内容是发言的真实对象）
            _bi_text = (message.text or message.caption or "")
            try:
                _rq = getattr(message, "reply_to_message", None)
                if _rq is not None:
                    _qt = (getattr(_rq, "text", None)
                           or getattr(_rq, "caption", None) or "")
                    if _qt:
                        _bi_text = f"{_bi_text} [引用] {str(_qt)[:200]}"
            except Exception:
                pass
            _bi = trigger_verdict(
                _bi_cfg if isinstance(_bi_cfg, dict) else {},
                getattr(getattr(message, "chat", None), "id", ""),
                getattr(getattr(message, "from_user", None), "id", ""),
                _bi_text,
                has_photo=bool(getattr(message, "photo", None)),
                is_direct=_bi_direct,
            )
            if _bi is not None:
                if _bi:
                    message._trigger_path = "bug_intake"
                    self.logger.info("[bug_intake] 报障群引燃回复")
                else:
                    self.logger.info("[bug_intake] 报障群压制（限频/危机词/闲聊/纯图）")
                    # B33（实施49 2026-08-21）：被压制但带图 → 图仍入库。
                    # 文字可 sync 还原、媒体不可——限频/闲聊误拦真反馈时，
                    # 截图证据此前随压制永久丢失（06:33-06:38 实录 6 条）。
                    # 后台归档到 protocol_media + bug_events 落 URL，不回复不引燃。
                    if getattr(message, "photo", None):
                        try:
                            asyncio.create_task(
                                self._bug_intake_archive_capped_photo(message))
                        except Exception:
                            self.logger.debug(
                                "[bug_intake] 压制截图归档任务创建失败", exc_info=True)
                return _bi
        except Exception:
            self.logger.debug("[bug_intake] 触发判定异常（放行原链）",
                              exc_info=True)
        if self.user_info and self._should_reply_by_reply_chain(message):
            message._trigger_path = "reply_chain"
            return True
        if self._contains_mention_of_self(message):
            self.logger.info("[@本账号] 消息中 @ 了当前登录账号，判定应回复")
            message._trigger_path = "mention"
            return True
        text = (message.text or message.caption or "").strip()
        trigger_config = self.config.get('trigger', {})
        if trigger_config.get('enabled', False) and self.four_layer_trigger:
            if await self._should_reply_with_four_layer_trigger(message):
                return True
            if text and await self._should_reply_by_follow_up_context(message, text):
                message._trigger_path = "follow_up"
                return True
            if text and await self._should_reply_by_l2_fallback(message, text):
                message._trigger_path = "l2_fallback"
                return True
            if text and await self._should_reply_by_ai_context(message, text):
                message._trigger_path = "ai_context"
                return True
            return False
        if self._should_reply_with_legacy_method(message):
            message._trigger_path = "legacy"
            return True
        if text and await self._should_reply_by_follow_up_context(message, text):
            message._trigger_path = "follow_up"
            return True
        if text and await self._should_reply_by_l2_fallback(message, text):
            message._trigger_path = "l2_fallback"
            return True
        if text and await self._should_reply_by_ai_context(message, text):
            message._trigger_path = "ai_context"
            return True
        return False

    async def _should_reply_with_four_layer_trigger(self, message) -> bool:
        try:
            text = message.text or message.caption or ""
            has_image = bool(message.photo or (message.document and
                         message.document.mime_type and
                         message.document.mime_type.startswith('image/')))
            has_document = bool(message.document)
            if not text and (message.voice or message.audio or has_image):
                return False
            user_id = message.from_user.id if message.from_user else 0
            username = message.from_user.username if message.from_user else "unknown"
            chat_id = f"group_{message.chat.id}" if message.chat.id else "unknown"
            my_username = getattr(self.user_info, "username", None) if self.user_info else None
            should_reply, decision_details = await self.four_layer_trigger.should_reply(
                message_text=text,
                chat_id=chat_id,
                user_id=str(user_id),
                username=username,
                message_type="text",
                has_image=has_image,
                has_document=has_document,
                bot_username=my_username,
            )
            if decision_details.get('final_decision', False):
                reason = decision_details.get('reason', '未知原因')
                if 'L1' in reason:
                    message._trigger_path = "l1_rule"
                elif 'L2' in reason:
                    message._trigger_path = "l2_semantic"
                else:
                    message._trigger_path = "four_layer"
                self.logger.info(
                    "四层触发决策: 回复 - %s, 置信度: %.3f",
                    reason,
                    decision_details.get('layers', {}).get('l2', {}).get('confidence', 0),
                )
            else:
                reason = decision_details.get('reason', '未知原因')
                self.logger.info(
                    "四层触发决策: 不回复 - %s (若需回复可: 回复我们的消息 / @本账号 / 关键词或图片+文字 / 追问或会话窗口内L2)",
                    reason,
                )
            return should_reply
        except Exception as e:
            self.logger.error("四层触发决策失败: %s", e)
            return False

    def _should_reply_with_legacy_method(self, message) -> bool:
        group_config = self.config.get('telegram', {}).get('group_reply', {})
        # 缺省从 always 改 mention_or_keyword（P3-2，2026-07-25 实测事故）：
        # 实例基线 config 漂移丢 mode 键时，always 会让群里每句都回且短路
        # 全部兜底闸——缺配置=部署不完整，宁静默勿刷屏。要旧行为请显式配。
        mode = group_config.get('mode', 'mention_or_keyword')
        if mode == 'always':
            return True
        text = message.text or message.caption or ""
        if not text:
            return False
        if mode == 'mention_only':
            return self._contains_mention(text, group_config)
        if mode == 'keyword_only':
            return self._contains_keyword(text, group_config)
        if mode == 'mention_or_keyword':
            return (self._contains_mention(text, group_config) or
                    self._contains_keyword(text, group_config))
        return True

    def _contains_mention(self, text: str, group_config: dict) -> bool:
        usernames = group_config.get('mention_usernames', [])
        for username in usernames:
            clean_username = username.lstrip('@')
            if clean_username.lower() in text.lower():
                return True
            if username.lower() in text.lower():
                return True
        return False

    def _contains_keyword(self, text: str, group_config: dict) -> bool:
        keywords = group_config.get('keywords', [])
        case_sensitive = group_config.get('case_sensitive', False)
        require_exact = group_config.get('require_exact_match', False)
        if not case_sensitive:
            text = text.lower()
        for keyword in keywords:
            keyword_check = keyword if case_sensitive else keyword.lower()
            if require_exact:
                if text == keyword_check:
                    return True
            else:
                if keyword_check in text:
                    return True
        return False
