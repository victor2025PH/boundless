# -*- coding: utf-8 -*-
"""
AI 模型客户端 — 支持 DeepSeek / Qwen / OpenAI / 智谱 GLM 兼容 API。

通过 OpenAI 兼容协议调用各模型，解析用户中文指令为结构化操作。
核心改进（P0-1）:
  - SYSTEM_PROMPT 新增 live_engage / plan_followers / campaign_playbook / comment_engage
  - JSON 输出新增 targeting 块（gender / age_min / age_max / interests）
  - _multi_intent_parse 新增直播/评论/涨粉/人群画像关键词
  - _extract_gender / _extract_age_range / _extract_interests 辅助方法
  - 目标驱动规划：自然语言"找X人" → plan_followers，"涨粉X万" → plan_followers
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import yaml

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

_CONFIG_PATH = config_file("chat.yaml")


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  系统提示词 — 能力表 + 精准投放字段 + 输出格式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = """\
你是 OpenClaw 手机自动化助手。用户用中文给你指令，你解析后返回**严格 JSON**（不要任何多余文字）。

## 可用操作

| intent | 说明 | 关键参数 |
|--------|------|---------|
| warmup | 养号/刷视频/浏览 | duration_minutes, target_country, phase(auto/cold_start/interest_building/active) |
| follow | 精准关注目标用户 | max_follows, target_country, keyword, seed_accounts |
| live_engage | 进入直播间发评论+关注活跃观众 | target_country, max_live_rooms(默认3), comments_per_room(默认2), follow_active_viewers |
| comment_engage | 热门视频评论区互动+关注评论者 | target_country, keyword, max_videos(默认5), comments_per_video(默认3) |
| campaign_playbook | 完整获客剧本(warmup→live→follow→dm自动编排) | target_country, steps, goals |
| plan_followers | 涨粉目标规划(估算天数+每日SOP) | target_followers, target_country |
| test_follow | 测试关注能力 | (无) |
| check_inbox | 检查收件箱/AI自动回复 | auto_reply, max_conversations |
| send_dm | 发私信（需指定收件人） | recipient, message |
| vpn_setup | 配置VPN | uri_or_qr, mode(global/perapp) |
| vpn_status | VPN状态 | (无) |
| vpn_stop | 停止VPN | device_id |
| vpn_reconnect | 重连VPN | (无) |
| device_list | 设备列表/在线状态 | (无) |
| stats | 统计/数据/进度/漏斗 | (无) |
| health | 系统健康/掉线检查 | (无) |
| risk | 风险等级/账号安全 | device_id |
| schedule_list | 查看定时任务 | (无) |
| schedule_create | 创建定时任务 | cron, task_type |
| geo_check | 检查IP/地理位置 | device_id |
| leads | 线索/CRM数据 | (无) |
| stop_all | 紧急停止全部 | (无) |
| set_referral | 设置引流账号 | telegram, whatsapp |
| switch_country | 切换目标国家 | target_country |
| daily_report | 今日日报 | (无) |
| help | 帮助/功能列表 | (无) |

## 精准投放 — targeting 块（可选）
当用户指定性别/年龄/兴趣时，在 targeting 块中填写，不要把这些信息塞进 phase 字段：
- gender: "male" 或 "female"（用户说"男性/male"→male，"女性/female"→female）
- age_min: 最小年龄整数（用户说"20岁以上"→20，"30+"→30）
- age_max: 最大年龄整数（用户说"20-25岁"→25）
- interests: 兴趣标签数组，如 ["beauty", "business", "fitness"]
- min_followers: 最小粉丝数整数（用户说"30万粉丝以上"→300000）

## 目标声明 — goals 块（可选）
- followers: 目标新增粉丝数（"涨粉30万"→300000）
- dms: 目标私信数
- live_exposure: 目标直播曝光人次

## 设备映射
{device_map}

- "所有手机"/"全部"/"all"/"每台" → devices=["all"]
- 未指定设备且操作需要设备 → devices=["all"]
- "01-05号手机" → 解析为对应序列号数组

## phase 字段说明（仅 warmup 使用，不要用于其他意图）
- auto: 系统自动判断（推荐）
- cold_start: 新号冷启动，低互动
- interest_building: 兴趣建立期，搜索hashtag
- active: 活跃维持期
**禁止**把人群描述（女性、20岁等）放入 phase！人群描述放入 targeting 块。

## 输出格式示例

普通养号：
{{"intent": "warmup", "devices": ["all"], "params": {{"duration_minutes": 30, "target_country": "italy"}}}}

找菲律宾20-25岁女性用户进行直播互动：
{{"intent": "live_engage", "devices": ["all"], "params": {{"target_country": "philippines", "max_live_rooms": 5, "follow_active_viewers": true}}, "targeting": {{"gender": "female", "age_min": 20, "age_max": 25}}}}

关注菲律宾女性用户：
{{"intent": "follow", "devices": ["all"], "params": {{"max_follows": 50, "target_country": "philippines"}}, "targeting": {{"gender": "female", "age_min": 20, "age_max": 25}}}}

涨粉30万规划：
{{"intent": "plan_followers", "devices": [], "params": {{"target_country": "philippines"}}, "targeting": {{"gender": "female", "age_min": 20, "age_max": 25}}, "goals": {{"followers": 300000}}}}

完整获客剧本（养号+直播+关注+私信）：
{{"intent": "campaign_playbook", "devices": ["all"], "params": {{"target_country": "philippines", "steps": ["warmup", "live_engage", "follow", "check_inbox"]}}, "targeting": {{"gender": "female", "age_min": 20, "age_max": 25}}}}

多设备范围：
{{"intent": "warmup", "devices": ["SERIAL1", "SERIAL2"], "params": {{"duration_minutes": 30, "target_country": "italy"}}}}

help：
{{"intent": "help", "devices": [], "params": {{}}}}
"""


class ChatAI:
    """AI 模型客户端，通过 OpenAI 兼容 API 解析聊天指令。"""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or _load_config()
        ai_cfg = cfg.get("ai", {})

        self._provider = ai_cfg.get("provider", "deepseek")
        self._model = ai_cfg.get("model", "deepseek-chat")
        self._base_url = ai_cfg.get("base_url", "https://api.deepseek.com/v1")
        self._temperature = ai_cfg.get("temperature", 0.05)
        self._max_tokens = ai_cfg.get("max_tokens", 1000)

        api_key = ai_cfg.get("api_key", "")
        if api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY",
                     os.environ.get("QWEN_API_KEY",
                     os.environ.get("OPENAI_API_KEY",
                     os.environ.get("ZHIPU_API_KEY", ""))))

        self._api_key = api_key

        aliases = cfg.get("device_aliases", {})
        self._device_map = {str(k): v for k, v in aliases.items()}
        self._defaults = cfg.get("defaults", {})

        self._system_prompt = self._build_system_prompt()
        self._history: List[dict] = []

    def _build_system_prompt(self) -> str:
        lines = []
        for alias, serial in sorted(self._device_map.items()):
            lines.append(f'- "{alias}号"/"Phone-{alias}" = {serial}')
        device_map_str = "\n".join(lines) if lines else "- 无设备配置"
        return SYSTEM_PROMPT.format(device_map=device_map_str)

    def parse_intent(self, user_message: str) -> Dict[str, Any]:
        """
        Send user message to AI and parse intent.
        Returns {"intent": str, "devices": list, "params": dict, "targeting": dict, "goals": dict}.
        Falls back to local parsing if AI unavailable.
        """
        if not self._api_key:
            log.warning("[ChatAI] No API key, falling back to local parsing")
            return self._local_parse(user_message)

        try:
            return self._call_api(user_message)
        except Exception as e:
            log.error("[ChatAI] API call failed: %s, falling back to local", e)
            return self._local_parse(user_message)

    def parse_unified(self, user_message: str) -> Optional[Dict[str, Any]]:
        """
        单次 LLM：routing（分流）+ execute 时的 intent/params（与 parse_intent 字段对齐）。
        失败返回 None，由 ChatController 回退到 triage + parse_intent。
        """
        if not self._api_key:
            return None
        try:
            return self._call_unified_api(user_message)
        except Exception as e:
            log.warning("[ChatAI] unified parse failed: %s", e)
            return None

    def _call_unified_api(self, user_message: str) -> Optional[Dict[str, Any]]:
        import urllib.request

        from src.chat.unified_parse import build_unified_system_prompt, normalize_unified_payload

        system = build_unified_system_prompt(self._system_prompt)
        messages = [{"role": "system", "content": system}]
        for h in self._history[-6:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        max_tok = max(self._max_tokens, 1600)
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": max_tok,
            "response_format": {"type": "json_object"},
        }

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())

        content = result["choices"][0]["message"]["content"]
        log.debug("[ChatAI] Unified raw: %s", content[:500])

        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": content})

        parsed = json.loads(content)
        normalized = normalize_unified_payload(parsed)
        if normalized is None:
            log.warning("[ChatAI] unified JSON invalid or failed validation")
            return None
        return normalized

    def generate_reply(self, intent_result: Dict, action_results: List[dict],
                       user_message: str) -> str:
        """Generate a Chinese reply based on action results."""
        intent = intent_result.get("intent", "")
        targeting = intent_result.get("targeting", {})
        goals = intent_result.get("goals", {})

        if intent == "help":
            return self._help_text()

        if not action_results:
            return "操作已提交。"

        # 构造人群描述前缀（如有targeting）
        targeting_desc = self._format_targeting_desc(targeting)

        parts = []
        for r in action_results:
            dev_label = r.get("device_label") or r.get("device", "")
            prefix = f"[{dev_label}] " if dev_label and dev_label != "all" else ""
            if r.get("error"):
                parts.append(f"{prefix}[失败] {r['error']}")
            elif r.get("data"):
                parts.append(f"{prefix}{self._format_data(intent, r['data'])}")
            elif r.get("task_id"):
                tids = r.get("task_ids")
                if isinstance(tids, list) and tids and r.get("device_labels"):
                    n = len(tids)
                    lbl_preview = ", ".join(r["device_labels"][:4])
                    if len(r["device_labels"]) > 4:
                        lbl_preview += "…"
                    id_preview = ", ".join(str(x)[:8] for x in tids[:4])
                    if n > 4:
                        id_preview += f"…（共{n}个）"
                    task_desc = _INTENT_DISPLAY_NAMES.get(intent, intent)
                    targeting_suffix = f"  人群：{targeting_desc}" if targeting_desc else ""
                    parts.append(
                        f"[批量×{n}] {lbl_preview}\n"
                        f"  任务：{task_desc}（ID: {id_preview}）{targeting_suffix}"
                    )
                else:
                    tid_one = str(r["task_id"])
                    task_desc = _INTENT_DISPLAY_NAMES.get(intent, intent)
                    targeting_suffix = f"，人群：{targeting_desc}" if targeting_desc else ""
                    parts.append(f"{prefix}✓ {task_desc}任务已创建 (ID: {tid_one[:8]}...){targeting_suffix}")
            elif r.get("action") == "plan_followers":
                data = r.get("data", {})
                parts.append(data.get("message", "涨粉规划已生成"))
            else:
                parts.append("操作成功")

        reply = "\n".join(parts)

        # 附加目标说明（若有 goals）
        if goals.get("followers"):
            reply += f"\n\n目标：新增粉丝 {goals['followers']:,} 人"
        if goals.get("dms"):
            reply += f"\n目标：发送 {goals['dms']} 条私信"

        return reply

    def _format_targeting_desc(self, targeting: dict) -> str:
        if not targeting:
            return ""
        parts = []
        gender_map = {"male": "男性", "female": "女性"}
        g = targeting.get("gender", "")
        if g in gender_map:
            parts.append(gender_map[g])
        age_min = targeting.get("age_min", 0)
        age_max = targeting.get("age_max", 0)
        if age_min and age_max:
            parts.append(f"{age_min}-{age_max}岁")
        elif age_min:
            parts.append(f"{age_min}岁以上")
        elif age_max:
            parts.append(f"{age_max}岁以下")
        mf = targeting.get("min_followers", 0)
        if mf:
            if mf >= 10000:
                parts.append(f"{mf//10000}万粉以上")
            else:
                parts.append(f"{mf}粉以上")
        interests = targeting.get("interests", [])
        if interests:
            parts.append(f"兴趣:{'/'.join(interests[:3])}")
        return "，".join(parts) if parts else ""

    def _call_api(self, user_message: str) -> Dict[str, Any]:
        """Call AI API via OpenAI-compatible protocol."""
        import urllib.request

        messages = [
            {"role": "system", "content": self._system_prompt},
        ]
        for h in self._history[-6:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        body = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        content = result["choices"][0]["message"]["content"]
        log.debug("[ChatAI] Raw response: %s", content)

        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": content})

        parsed = json.loads(content)
        return {
            "intent": parsed.get("intent", "help"),
            "devices": parsed.get("devices", []),
            "params": parsed.get("params", {}),
            "targeting": parsed.get("targeting", {}),
            "goals": parsed.get("goals", {}),
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  多意图本地解析（LLM 不可用时的降级）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _multi_intent_parse(self, msg: str) -> Dict[str, Any]:
        """多意图解析 — 一句话包含多个操作时拆分为 intents 数组。
        也处理人群画像（gender/age）和目标声明（goals）。
        """
        devices = self._extract_devices(msg)
        country = self._extract_country(msg)
        targeting = self._extract_targeting(msg)
        goals = self._extract_goals(msg)
        intents = []

        # 拆分逗号/顿号/分号分隔的子句
        clauses = re.split(r'[,，、;；]+', msg)

        for clause in clauses:
            cl = clause.lower().strip()
            if not cl:
                continue

            # ★ 优先检测多步骤剧本（避免被 warmup/follow 提前捕获）
            if any(w in cl for w in ["剧本", "全流程", "campaign", "战役", "获客流程"]):
                steps = ["warmup"]
                if any(w in cl for w in ["直播", "live"]):
                    steps.append("live_engage")
                if any(w in cl for w in ["关注", "follow"]):
                    steps.append("follow")
                if any(w in cl for w in ["私信", "收件箱", "dm", "inbox"]):
                    steps.append("check_inbox")
                if len(steps) < 2:
                    steps = ["warmup", "live_engage", "follow", "check_inbox"]
                intents.append({"action": "tiktok_campaign_playbook",
                                "params": {"target_country": country, "steps": steps}})
                continue

            if any(w in cl for w in ["涨粉", "增粉", "粉丝目标"]):
                target_f = goals.get("followers") or self._extract_count(
                    clause, ["粉", "粉丝", "人"]) or 0
                if "万" in clause:
                    num_m = re.search(r'(\d+(?:\.\d+)?)\s*万', clause)
                    if num_m:
                        target_f = int(float(num_m.group(1)) * 10000)
                intents.append({"action": "plan_followers",
                                "params": {"target_followers": target_f, "target_country": country}})
                continue

            # ★ P3-2: 扩展关键词集，覆盖语音输入错字/同义词/口语表达
            # 直播类优先检测（含语音错字：活行≈直播，活跃互动≈直播互动）
            _live_kws = [
                "直播", "live", "live_engage", "直播间", "进直播",
                "活行互动", "活跃互动", "在播", "livestream", "播主", "主播互动",
                "直播评论", "直播间评论", "直播互动", "进播", "进入直播",
            ]
            # 评论区关键词
            _comment_kws = [
                "评论区", "comment_engage", "评论互动", "评论抓人",
                "评论区互动", "评论区关注", "热门视频评论", "视频评论",
                "评论区找人", "评论找人",
            ]
            # 养号关键词（必须明确有养号词才触发，不因"30分钟"误判）
            _warmup_kws = ["养号", "warmup", "刷视频", "冷启动", "预热", "浏览视频"]

            if any(w in cl for w in _live_kws):
                rooms = self._extract_count(clause, ["间", "个"]) or 3
                intents.append({"action": "tiktok_live_engage",
                                "params": {
                                    "target_country": country,
                                    "max_live_rooms": min(rooms, 10),
                                    "follow_active_viewers": True,
                                }})

            elif any(w in cl for w in _comment_kws):
                cnt = self._extract_count(clause, ["个", "条", "视频"]) or 5
                intents.append({"action": "tiktok_comment_engage",
                                "params": {
                                    "target_country": country,
                                    "max_videos": min(cnt, 20),
                                }})

            elif any(w in cl for w in _warmup_kws):
                dur = self._extract_duration(clause) or 30
                intents.append({"action": "tiktok_warmup",
                                "params": {"duration_minutes": dur, "target_country": country}})

            elif any(w in cl for w in ["关注", "follow", "添加好友", "加好友", "找人", "找用户"]):
                count = self._extract_count(clause, ["人", "个"]) or 20
                intents.append({"action": "tiktok_follow",
                                "params": {"max_follows": count, "target_country": country,
                                           "country": country}})

            elif any(w in cl for w in ["引流", "消息", "聊天", "dm", "私信", "对话"]):
                count = self._extract_count(clause, ["人", "个", "条", "次"]) or 10
                if any(w in cl for w in ["引流"]) and count >= 100:
                    intents.append({"action": "plan_referral",
                                    "params": {"target_messages": count, "country": country}})
                elif self._clause_suggests_explicit_dm(clause):
                    intents.append({"action": "tiktok_chat",
                                    "params": {"max_chats": max(1, count)}})
                else:
                    mc = max(5, min(count if count else 20, 50))
                    intents.append({"action": "tiktok_check_inbox",
                                    "params": {"auto_reply": True, "max_conversations": mc}})

            elif any(w in cl for w in ["评论监控", "监控评论", "启动监控", "comment monitor",
                                       "开启监控", "评论回复监控", "自动回复评论"]):
                # ★ P3-3: 必须放在通用"评论"判断之前
                enabled = not any(w in cl for w in ["关闭", "停止", "暂停", "disable", "off"])
                intents.append({"action": "comment_monitor_on" if enabled else "comment_monitor_off",
                                "params": {}})

            elif any(w in cl for w in ["评论", "comment"]):
                intents.append({"action": "tiktok_warmup",
                                "params": {"duration_minutes": 10, "comment_mode": True,
                                           "target_country": country}})

            elif any(w in cl for w in ["涨粉", "增粉", "粉丝目标", "粉丝数"]):
                target_f = goals.get("followers") or self._extract_count(
                    clause, ["粉", "粉丝", "人", "万"]) or 0
                if "万" in clause:
                    num_m = re.search(r'(\d+(?:\.\d+)?)\s*万', clause)
                    if num_m:
                        target_f = int(float(num_m.group(1)) * 10000)
                intents.append({"action": "plan_followers",
                                "params": {"target_followers": target_f, "target_country": country}})

        # 无意图时回退到旧解析
        if len(intents) == 0:
            return self._local_parse(msg)
        # 单意图直接返回
        if len(intents) == 1:
            single = intents[0]
            return {
                "intent": single["action"],
                "devices": devices,
                "params": {**{"target_country": country}, **single.get("params", {})},
                "targeting": targeting,
                "goals": goals,
            }

        return {
            "intent": "multi_task",
            "devices": devices,
            "params": {"target_country": country},
            "targeting": targeting,
            "goals": goals,
            "intents": intents,
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  精准投放提取
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _extract_targeting(self, msg: str) -> Dict[str, Any]:
        """从用户消息提取人群定向参数。"""
        t: Dict[str, Any] = {}
        ml = msg.lower()

        # 性别
        gender = self._extract_gender(msg)
        if gender:
            t["gender"] = gender

        # 年龄范围
        age_min, age_max = self._extract_age_range(msg)
        if age_min:
            t["age_min"] = age_min
        if age_max:
            t["age_max"] = age_max

        # 粉丝数下限
        min_f = self._extract_min_followers(msg)
        if min_f:
            t["min_followers"] = min_f

        # 兴趣标签
        interests = self._extract_interests(msg)
        if interests:
            t["interests"] = interests

        return t

    def _extract_gender(self, msg: str) -> Optional[str]:
        ml = msg.lower()
        female_kws = ["女性", "女生", "女人", "女", "female", "woman", "girl", "girls", "women"]
        male_kws = ["男性", "男生", "男人", "男", "male", "man", "boy", "boys", "men"]
        for kw in female_kws:
            if kw in ml:
                return "female"
        for kw in male_kws:
            if kw in ml:
                return "male"
        return None

    def _extract_age_range(self, msg: str) -> tuple:
        """返回 (age_min, age_max)，提取不到则返回 (0, 0)。"""
        # "20-25岁" / "20至25岁" / "20到25岁"（有"岁"后缀）
        m = re.search(r'(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*岁', msg)
        if m:
            return int(m.group(1)), int(m.group(2))
        # ★ P0 Fix: "20-25" / "20~25" 无"岁"但处于合理年龄范围
        # 使用负向前后瞻替代 \b（\b 在中文字符后无效）
        m = re.search(r'(?<!\d)(1[89]|2\d|3\d|4\d|5\d)\s*[-~]\s*(1[89]|2\d|3\d|4\d|5\d)(?!\d)', msg)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 18 <= a < b <= 60:
                return a, b
        # "20岁以上" / "20+" / "30岁以上" / "20以上"
        m = re.search(r'(\d{1,2})\s*岁?\s*(?:以上|以上|plus|\+)', msg)
        if m:
            age = int(m.group(1))
            if age >= 18:
                return age, 0
        # "25岁以下"
        m = re.search(r'(\d{1,2})\s*岁?\s*以下', msg)
        if m:
            age = int(m.group(1))
            if age >= 18:
                return 0, age
        # 单独 "30岁" → 视为 min_age
        m = re.search(r'(\d{1,2})\s*岁', msg)
        if m:
            age = int(m.group(1))
            if age >= 18:
                return age, 0
        return 0, 0

    def _extract_min_followers(self, msg: str) -> int:
        """提取粉丝数下限，如 "30万粉"→300000, "10000粉"→10000。"""
        # "30万粉丝" / "30万以上粉丝"
        m = re.search(r'(\d+(?:\.\d+)?)\s*万\s*(?:粉丝?|followers?|以上粉?)', msg)
        if m:
            return int(float(m.group(1)) * 10000)
        # 直接数字 "10000粉"
        m = re.search(r'(\d+)\s*(?:粉丝?|followers?)', msg)
        if m:
            return int(m.group(1))
        return 0

    def _extract_interests(self, msg: str) -> List[str]:
        """从消息提取兴趣标签。"""
        interest_map = {
            "美妆": "beauty", "护肤": "beauty", "化妆": "beauty",
            "健身": "fitness", "运动": "fitness", "sport": "fitness",
            "商业": "business", "创业": "business", "生意": "business",
            "美食": "food", "料理": "food", "cooking": "food",
            "旅游": "travel", "旅行": "travel", "travel": "travel",
            "时尚": "fashion", "潮流": "fashion", "fashion": "fashion",
            "科技": "tech", "技术": "tech", "tech": "tech",
            "音乐": "music", "唱歌": "music", "music": "music",
            "游戏": "gaming", "电竞": "gaming", "game": "gaming",
        }
        ml = msg.lower()
        found = []
        for kw, tag in interest_map.items():
            if kw in ml and tag not in found:
                found.append(tag)
        return found

    def _extract_goals(self, msg: str) -> Dict[str, Any]:
        """提取目标声明，如"涨粉30万"→{"followers": 300000}。"""
        goals: Dict[str, Any] = {}
        # 涨粉 X 万
        m = re.search(r'涨粉\s*(\d+(?:\.\d+)?)\s*万', msg)
        if m:
            goals["followers"] = int(float(m.group(1)) * 10000)
        elif re.search(r'(\d+(?:\.\d+)?)\s*万\s*粉丝', msg):
            mm = re.search(r'(\d+(?:\.\d+)?)\s*万\s*粉丝', msg)
            if mm:
                goals["followers"] = int(float(mm.group(1)) * 10000)
        # 发 X 条消息
        m = re.search(r'发\s*(\d+)\s*条', msg)
        if m:
            goals["dms"] = int(m.group(1))
        return goals

    def _clause_suggests_explicit_dm(self, clause: str) -> bool:
        """是否像「发给具体对象」的私信。"""
        s = (clause or "").strip()
        if "@" in s:
            return True
        if "私信给" in s or "发给" in s or "dm给" in s.lower():
            return True
        if "给" in s and any(x in s for x in ["用户", "他", "她", "对方"]):
            return True
        return False

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  本地规则解析（LLM 彻底不可用的最终降级）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _local_parse(self, msg: str) -> Dict[str, Any]:
        """Rule-based fallback parser for when AI is unavailable."""
        ml = msg.lower().strip()
        result: Dict[str, Any] = {
            "intent": "help", "devices": [], "params": {},
            "targeting": {}, "goals": {},
        }

        devices = self._extract_devices(msg)
        result["devices"] = devices
        targeting = self._extract_targeting(msg)
        if targeting:
            result["targeting"] = targeting
        goals = self._extract_goals(msg)
        if goals:
            result["goals"] = goals

        if any(w in ml for w in ["帮助", "help", "你能做什么", "功能"]):
            result["intent"] = "help"
        elif any(w in ml for w in ["养号", "warmup", "刷视频", "刷抖音"]):
            result["intent"] = "warmup"
            dur = self._extract_duration(msg)
            result["params"]["duration_minutes"] = dur or self._defaults.get("warmup_duration", 30)
            result["params"]["target_country"] = self._extract_country(msg)
        elif any(w in ml for w in ["直播间", "进直播", "live_engage",
                                    "进入直播", "直播互动", "直播评论"]):
            result["intent"] = "live_engage"
            result["params"]["target_country"] = self._extract_country(msg)
            rooms = self._extract_count(msg, ["间", "个"]) or 3
            result["params"]["max_live_rooms"] = min(rooms, 10)
            result["params"]["follow_active_viewers"] = True
        elif any(w in ml for w in ["评论区互动", "comment_engage", "评论抓人"]):
            result["intent"] = "comment_engage"
            result["params"]["target_country"] = self._extract_country(msg)
        elif any(w in ml for w in ["涨粉", "增粉", "粉丝目标"]):
            result["intent"] = "plan_followers"
            result["params"]["target_country"] = self._extract_country(msg)
            result["params"]["target_followers"] = goals.get("followers", 0)
        elif any(w in ml for w in ["完整获客", "全流程", "campaign", "剧本", "战役"]):
            result["intent"] = "campaign_playbook"
            result["params"]["target_country"] = self._extract_country(msg)
            result["params"]["steps"] = ["warmup", "live_engage", "follow", "check_inbox"]
        elif any(w in ml for w in ["关注", "follow", "粉丝"]) and any(
                w in ml for w in ["测试", "test", "能不能", "检查"]):
            result["intent"] = "test_follow"
        elif any(w in ml for w in ["关注", "follow", "粉丝"]):
            result["intent"] = "follow"
            max_f = self._extract_count(msg, ["个", "人"])
            if max_f:
                result["params"]["max_follows"] = max_f
            result["params"]["target_country"] = self._extract_country(msg)
        elif any(w in ml for w in ["发消息", "发私信", "send_dm", "发信息"]):
            result["intent"] = "send_dm"
        elif any(w in ml for w in ["收件箱", "inbox", "私信检查", "检查消息",
                                    "查看消息", "看消息"]):
            result["intent"] = "check_inbox"
        elif any(w in ml for w in ["配置vpn", "换vpn", "vpn配置", "vpn setup",
                                    "设置vpn", "全局vpn"]):
            result["intent"] = "vpn_setup"
            if "全局" in ml:
                result["params"]["mode"] = "global"
        elif any(w in ml for w in ["停vpn", "停掉", "关vpn", "vpn停", "vpn关"]) and "vpn" in ml:
            result["intent"] = "vpn_stop"
        elif any(w in ml for w in ["重连vpn", "vpn重连", "reconnect"]):
            result["intent"] = "vpn_reconnect"
        elif "vpn" in ml:
            result["intent"] = "vpn_status"
        elif any(w in ml for w in ["设备", "手机", "在线", "device"]) and any(
                w in ml for w in ["列表", "哪些", "状态", "几台"]):
            result["intent"] = "device_list"
        elif any(w in ml for w in ["线索", "leads", "crm"]):
            result["intent"] = "leads"
        elif any(w in ml for w in ["壁纸", "wallpaper", "编号壁纸"]):
            result["intent"] = "set_wallpaper"
        elif any(w in ml for w in ["统计", "数据", "stats", "进度", "漏斗"]):
            result["intent"] = "stats"
        elif any(w in ml for w in ["健康", "health", "掉线"]):
            result["intent"] = "health"
        elif any(w in ml for w in ["风险", "risk", "安全"]):
            result["intent"] = "risk"
        elif any(w in ml for w in ["定时", "schedule", "自动任务", "cron"]):
            result["intent"] = "schedule_create" if any(
                w in ml for w in ["创建", "新建", "添加"]) else "schedule_list"
        elif any(w in ml for w in ["ip", "geo", "地理", "位置"]):
            result["intent"] = "geo_check"
        elif any(w in ml for w in ["停止", "stop", "紧急"]):
            result["intent"] = "stop_all"
        elif any(w in ml for w in ["引流"]) and self._extract_count(msg, ["人", "个", "条"]) >= 50:
            result["intent"] = "plan_referral"
            result["params"]["target_messages"] = self._extract_count(msg, ["人", "个", "条"])
            result["params"]["target_country"] = self._extract_country(msg)
        elif any(w in ml for w in ["切换", "切到", "换到"]) and any(
                w in ml for w in ["意大利", "德国", "法国", "美国", "英国", "日本",
                                   "菲律宾", "italy", "germany", "france", "usa", "uk",
                                   "japan", "philippines"]):
            result["intent"] = "switch_country"
            result["params"]["target_country"] = self._extract_country(msg)
        elif any(w in ml for w in ["战役", "campaign", "引流计划", "创建引流"]):
            result["intent"] = "create_campaign"
        elif any(w in ml for w in ["日报", "report", "今日", "汇总"]):
            result["intent"] = "daily_report"

        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  提取辅助方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _extract_count(self, text: str, units: Optional[List[str]] = None) -> int:
        """从文本提取数量，支持自定义单位。"""
        units = units or ["人", "个", "条", "次", "台"]
        units_pattern = "|".join(re.escape(u) for u in units)
        m = re.search(rf'(\d+)\s*(?:{units_pattern})', text)
        return int(m.group(1)) if m else 0

    def _extract_devices(self, msg: str) -> List[str]:
        """Extract device IDs from user message."""
        if any(w in msg for w in ["所有", "全部", "all", "每台"]):
            return ["all"]

        range_match = re.search(r'(\d+)\s*[-~到]\s*(\d+)\s*号', msg)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            found = []
            for i in range(start, end + 1):
                alias = f"{i:02d}"
                if alias in self._device_map:
                    found.append(self._device_map[alias])
            if found:
                return found

        found = []
        ml = msg.lower()
        for alias, serial in self._device_map.items():
            a_int = alias.lstrip("0") or alias
            patterns = [
                f"{alias}号", f"{alias} 号",
                f"phone-{alias}", f"phone {alias}",
                f"手机{alias}", f"#{alias}",
                f"{a_int}号", f"{a_int} 号",
            ]
            if len(alias) == 2 and alias.startswith("0"):
                patterns.append(alias)
            for p in patterns:
                if p in ml:
                    if serial not in found:
                        found.append(serial)
                    break
        return found

    def _extract_duration(self, msg: str) -> Optional[int]:
        """Extract duration in minutes."""
        m = re.search(r"(\d+)\s*分钟", msg)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*min", msg, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*小时", msg)
        if m:
            return int(m.group(1)) * 60
        return None

    def _extract_country(self, msg: str) -> str:
        country_map = {
            # ★ P0 Fix: 补充 Filipino/Filipina 英文形容词
            "filipino": "philippines", "filipina": "philippines",
            "pinay": "philippines", "pinoy": "philippines",
            "意大利": "italy", "italy": "italy",
            "德国": "germany", "germany": "germany",
            "法国": "france", "france": "france",
            "西班牙": "spain", "spain": "spain",
            "巴西": "brazil", "brazil": "brazil",
            "日本": "japan", "japan": "japan",
            "菲律宾": "philippines", "philippines": "philippines",
            "美国": "usa", "usa": "usa", "america": "usa",
            "英国": "uk", "uk": "uk", "england": "uk",
            "泰国": "thailand", "thailand": "thailand",
            "越南": "vietnam", "vietnam": "vietnam",
            "印尼": "indonesia", "indonesia": "indonesia",
            "马来西亚": "malaysia", "malaysia": "malaysia",
        }
        ml = msg.lower()
        for cn, en in country_map.items():
            if cn in ml:
                return en
        return self._defaults.get("target_country", "italy")

    def _format_data(self, intent: str, data: Any) -> str:
        if isinstance(data, dict):
            parts = []
            for k, v in data.items():
                if isinstance(v, (list, dict)):
                    continue
                parts.append(f"  {k}: {v}")
            return "\n".join(parts) if parts else str(data)
        if isinstance(data, list):
            return f"共 {len(data)} 条记录"
        return str(data)

    def _help_text(self) -> str:
        return """OpenClaw AI 指令台 — 完整能力列表:

  养号:    "01号手机养号30分钟" / "所有手机冷启动养号"
  关注:    "关注菲律宾20-25岁女性用户50人" / "01号关注意大利男性"
  直播互动: "进入菲律宾直播间发评论+关注活跃观众" / "03号机直播间互动3个"
  评论区:  "对菲律宾热门视频评论区互动"
  完整剧本: "菲律宾女粉获客全流程" / "campaign 意大利男性"
  涨粉规划: "涨粉30万" / "菲律宾女性粉丝增长30万规划"
  收件箱:  "查看01收件箱" / "检查消息并AI自动回复"
  私信:   "给回关的人发消息"
  VPN:   "VPN状态" / "配置VPN" / "停掉01的VPN"
  设备:   "哪些手机在线" / "设备状态"
  统计:   "今天数据" / "漏斗数据"
  风控:   "01号风险等级" / "账号安全吗"
  IP:    "01号IP在哪" / "检查IP"
  定时:   "定时任务有哪些"
  线索:   "CRM数据" / "有多少线索"
  日报:   "今日日报"
  停止:   "全部停止" / "紧急停止"

精准定向示例：
  "找菲律宾20-25岁女性用户进入直播间互动"
  "关注菲律宾30万粉以上女性账号"
  "菲律宾女粉涨粉30万规划"

设备编号: 01-11号，或 "所有手机"
"""

    def clear_history(self):
        self._history.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_INTENT_DISPLAY_NAMES = {
    "warmup": "养号",
    "follow": "精准关注",
    "live_engage": "直播间互动",
    "comment_engage": "评论区互动",
    "campaign_playbook": "完整获客剧本",
    "plan_followers": "涨粉规划",
    "check_inbox": "收件箱",
    "send_dm": "发私信",
    "vpn_setup": "VPN配置",
    "stop_all": "紧急停止",
    "plan_referral": "引流规划",
    "multi_task": "组合任务",
}

_instance: Optional[ChatAI] = None
_lock = threading.Lock()


def get_chat_ai() -> ChatAI:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = ChatAI()
    return _instance
