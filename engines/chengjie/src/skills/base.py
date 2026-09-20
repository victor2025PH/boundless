"""
Skill base class — shared by all domain skills and generic skills.
Extracted from skill_manager.py to avoid circular imports.
"""

import random
from typing import Dict, Any, Optional

from src.utils.logger import LoggerMixin
from src.ai.ai_client import AIClient


class Skill(LoggerMixin):
    """Skill base class"""

    def __init__(self, config, ai_client: AIClient):
        self.config = config
        self.ai_client = ai_client
        self.name = self.__class__.__name__
        self.priority = 5

    def _get_strategy_overrides(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        strategy = context.get('_reply_strategy', {})
        so = {}
        if 'temperature' in strategy and strategy['temperature']:
            so['temperature'] = strategy['temperature']
        if 'max_tokens' in strategy and strategy['max_tokens']:
            so['max_tokens'] = strategy['max_tokens']
        if 'context_rounds' in strategy:
            so['context_rounds'] = strategy['context_rounds']
        if strategy.get('model'):
            so['model'] = strategy['model']
        if 'thinking_budget' in strategy:
            so['thinking_budget'] = strategy['thinking_budget']
        return so or None

    async def execute(
        self,
        text: str,
        user_id: str,
        context: Dict[str, Any]
    ) -> Optional[str]:
        raise NotImplementedError("子类必须实现execute方法")

    def _get_kb_store(self):
        try:
            from src.utils.kb_registry import get_kb_store
            return get_kb_store(self.config, require_exists=True)
        except Exception:
            return None

    def _kb_reply(self, template_key: str, **kwargs) -> Optional[str]:
        kb = self._get_kb_store()
        if kb:
            return kb.get_direct_reply(template_key, **kwargs)
        return None

    def _kb_fallback(self, intent: str, lang: str = "zh") -> Optional[str]:
        """意图兜底话术：只认运营在 KB 里显式配置的模板（{intent}_fallback /
        global_fallback），没配 → None＝本轮不回复。

        2026-08-15 起硬编码罐头池全部移除（「在呀，你说～」/_FALLBACK_EN 英文池/
        conversion 域寒暄池）——罐头兜底在 AI 全链失败时对任意消息答非所问
        （「可以不回复，不能乱回复」）。非中文会话同理：KB 模板是中文写的，
        发给外语客户＝语言错配，宁可沉默。
        """
        if lang and lang != "zh":
            return None
        kb = self._get_kb_store()
        if kb:
            return kb.get_fallback(intent)
        return None

    def _get_template_reply(self, template_name: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        reply = self._kb_reply(template_name, **(context or {}))
        if reply:
            return reply
        from src.utils.template_engine import render_template
        if hasattr(self.config, 'get_dynamic_templates_config'):
            templates_config = self.config.get_dynamic_templates_config() or {}
            template_list = templates_config.get(template_name)
            if template_list is None:
                templates_config = self.config.get_templates_config()
                template_list = templates_config.get(template_name, [])
        else:
            templates_config = self.config.get_templates_config()
            template_list = templates_config.get(template_name, [])
        if isinstance(template_list, str):
            template_list = [template_list]
        if template_list:
            raw = random.choice(template_list)
            return render_template(raw, context) if context else raw
        return None
