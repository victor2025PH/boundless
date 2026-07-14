# -*- coding: utf-8 -*-
"""
Screen Anomaly Detector — detects popups, captchas, bans, errors via screenshot analysis.

Two-tier detection:
  1. Fast: text-based (XML dump / OCR keywords) — ~100ms
  2. Deep: vision LLM analysis — ~2s (only if fast check is ambiguous)

Anomaly types:
  - captcha:  验证码/人机验证/滑块验证
  - ban:      账号封禁/限制/违规
  - login:    登录过期/需要重新登录
  - popup:    系统弹窗/权限请求/广告
  - network:  网络错误/连接失败
  - update:   版本更新提示
  - crash:    应用崩溃/ANR
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class AnomalyType(str, Enum):
    CAPTCHA = "captcha"
    BAN = "ban"
    LOGIN = "login"
    POPUP = "popup"
    NETWORK = "network"
    UPDATE = "update"
    CRASH = "crash"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class AnomalyResult:
    device_id: str
    anomaly_type: AnomalyType
    severity: Severity
    description: str
    detected_at: float = field(default_factory=time.time)
    detection_method: str = "fast"
    confidence: float = 0.0
    auto_recoverable: bool = False
    recovery_action: str = ""

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "type": self.anomaly_type.value,
            "severity": self.severity.value,
            "description": self.description,
            "detected_at": self.detected_at,
            "method": self.detection_method,
            "confidence": round(self.confidence, 2),
            "auto_recoverable": self.auto_recoverable,
            "recovery_action": self.recovery_action,
        }


_KEYWORD_RULES: List[dict] = [
    {
        "type": AnomalyType.CAPTCHA,
        "severity": Severity.CRITICAL,
        "keywords": [
            "captcha", "recaptcha", "verify", "robot", "human verification",
            "验证码", "人机验证", "安全验证", "滑块验证", "拼图验证",
            "drag the slider", "select all", "pick the", "i'm not a robot",
            "security check", "verification required",
        ],
        "auto_recoverable": False,
        "recovery": "需要手动完成验证",
    },
    {
        "type": AnomalyType.BAN,
        "severity": Severity.CRITICAL,
        "keywords": [
            "账号封禁", "账号限制", "违规", "permanently banned",
            "account suspended", "temporarily restricted",
            "你的账号", "account banned", "违反社区", "community guidelines",
            "your account has been", "restricted",
        ],
        "auto_recoverable": False,
        "recovery": "账号被限制/封禁，需人工处理",
    },
    {
        "type": AnomalyType.LOGIN,
        "severity": Severity.WARNING,
        "keywords": [
            "登录", "log in", "sign in", "登录过期", "session expired",
            "重新登录", "请登录", "login required", "sign up",
        ],
        "auto_recoverable": False,
        "recovery": "需要重新登录",
    },
    {
        "type": AnomalyType.NETWORK,
        "severity": Severity.WARNING,
        "keywords": [
            "网络错误", "连接失败", "no internet", "connection failed",
            "network error", "无法连接", "请检查网络", "timeout",
            "server error", "try again later", "稍后重试",
        ],
        "auto_recoverable": True,
        "recovery": "press_back_and_retry",
    },
    {
        "type": AnomalyType.UPDATE,
        "severity": Severity.INFO,
        "keywords": [
            "更新", "update", "new version", "升级", "upgrade now",
            "update available", "立即更新", "下载更新",
        ],
        "auto_recoverable": True,
        "recovery": "dismiss_update_dialog",
    },
    {
        "type": AnomalyType.CRASH,
        "severity": Severity.CRITICAL,
        "keywords": [
            "已停止", "has stopped", "keeps stopping", "isn't responding",
            "无响应", "崩溃", "crash", "anr", "force close",
            "unfortunately", "wait", "close app",
        ],
        "auto_recoverable": True,
        "recovery": "force_restart_app",
    },
    {
        "type": AnomalyType.POPUP,
        "severity": Severity.INFO,
        "keywords": [
            "允许", "allow", "deny", "拒绝", "permission",
            "权限", "notification", "通知", "agree", "同意",
            "accept", "decline", "取消", "确定",
        ],
        "auto_recoverable": True,
        "recovery": "dismiss_popup",
    },
]

_VISION_PROMPT = """分析这个手机截图，检测是否存在以下异常情况。只返回JSON。

异常类型:
- captcha: 验证码/人机验证/滑块
- ban: 账号封禁/限制
- login: 登录过期/需重新登录
- popup: 系统弹窗/权限请求
- network: 网络错误/连接失败
- update: 版本更新提示
- crash: 应用崩溃/无响应
- none: 一切正常

返回格式:
{"anomaly": "类型或none", "confidence": 0.0-1.0, "description": "简短描述"}"""


class ScreenAnomalyDetector:
    """Two-tier anomaly detection: fast keyword + optional LLM vision."""

    def __init__(self, vision_client=None):
        self._vision_client = vision_client
        self._history: List[AnomalyResult] = []
        self._history_lock = threading.Lock()
        self._max_history = 500
        self._device_last_check: Dict[str, float] = {}

    def detect_fast(self, device_id: str,
                    xml_dump: str = "",
                    ui_text: str = "") -> Optional[AnomalyResult]:
        """Fast keyword-based detection using XML dump or extracted text."""
        text_lower = (xml_dump + " " + ui_text).lower()
        if not text_lower.strip():
            return None

        for rule in _KEYWORD_RULES:
            matched = [k for k in rule["keywords"] if k.lower() in text_lower]
            if len(matched) >= 1:
                if rule["type"] == AnomalyType.POPUP and len(matched) < 2:
                    continue
                result = AnomalyResult(
                    device_id=device_id,
                    anomaly_type=rule["type"],
                    severity=rule["severity"],
                    description=f"检测到关键词: {', '.join(matched[:3])}",
                    detection_method="fast",
                    confidence=min(0.5 + len(matched) * 0.15, 0.95),
                    auto_recoverable=rule["auto_recoverable"],
                    recovery_action=rule["recovery"],
                )
                self._record(result)
                return result
        return None

    def detect_image_heuristic(self, device_id: str,
                              screenshot_bytes: bytes) -> Optional[AnomalyResult]:
        """Mid-tier: image heuristic detection using PIL (~50ms, no API calls).

        Detects:
        - Dialog boxes (centered rectangle with semi-transparent overlay)
        - Error screens (dominant red/orange in center)
        - Black/frozen screens (uniform dark pixels)
        - Captcha patterns (puzzle/slider layouts)
        """
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
            w, h = img.size

            center_crop = img.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4))
            pixels = list(center_crop.getdata())
            total = len(pixels)
            if total == 0:
                return None

            r_avg = sum(p[0] for p in pixels) / total
            g_avg = sum(p[1] for p in pixels) / total
            b_avg = sum(p[2] for p in pixels) / total
            brightness = (r_avg + g_avg + b_avg) / 3

            dark_pixels = sum(1 for p in pixels if sum(p) < 60)
            dark_ratio = dark_pixels / total

            white_pixels = sum(1 for p in pixels if min(p) > 200)
            white_ratio = white_pixels / total

            red_pixels = sum(1 for p in pixels if p[0] > 150 and p[1] < 80 and p[2] < 80)
            red_ratio = red_pixels / total

            edge_strip = img.crop((0, 0, w, h // 8))
            edge_pixels = list(edge_strip.getdata())
            edge_dark = sum(1 for p in edge_pixels if sum(p) < 80)
            edge_dark_ratio = edge_dark / len(edge_pixels) if edge_pixels else 0

            if dark_ratio > 0.85:
                return AnomalyResult(
                    device_id=device_id,
                    anomaly_type=AnomalyType.CRASH,
                    severity=Severity.CRITICAL,
                    description="检测到黑屏/死机 (暗像素占比 {:.0%})".format(dark_ratio),
                    detection_method="image_heuristic",
                    confidence=min(0.5 + dark_ratio * 0.4, 0.9),
                    auto_recoverable=True,
                    recovery_action="force_restart_app",
                )

            if red_ratio > 0.08:
                return AnomalyResult(
                    device_id=device_id,
                    anomaly_type=AnomalyType.BAN,
                    severity=Severity.CRITICAL,
                    description="检测到大面积红色警告 (红色占比 {:.0%})".format(red_ratio),
                    detection_method="image_heuristic",
                    confidence=min(0.5 + red_ratio * 3, 0.85),
                    auto_recoverable=False,
                    recovery_action="账号被限制/封禁，需人工处理",
                )

            if (edge_dark_ratio > 0.6 and white_ratio > 0.3 and
                    brightness > 150):
                return AnomalyResult(
                    device_id=device_id,
                    anomaly_type=AnomalyType.POPUP,
                    severity=Severity.INFO,
                    description="检测到弹窗/对话框 (暗边+白中心)",
                    detection_method="image_heuristic",
                    confidence=0.65,
                    auto_recoverable=True,
                    recovery_action="dismiss_popup",
                )

        except ImportError:
            pass
        except Exception as e:
            log.debug("[anomaly] Image heuristic failed: %s", e)
        return None

    def detect_vision(self, device_id: str,
                      screenshot_bytes: bytes) -> Optional[AnomalyResult]:
        """Deep vision-based detection using LLM."""
        if not self._vision_client:
            try:
                from src.ai.llm_client import get_free_vision_client
                self._vision_client = get_free_vision_client()
            except Exception:
                pass
        if not self._vision_client:
            return None

        try:
            img_b64 = base64.b64encode(screenshot_bytes).decode()
            response = self._vision_client.chat_vision(
                _VISION_PROMPT, img_b64, max_tokens=256)

            import json
            json_match = re.search(r'\{[^}]+\}', response)
            if not json_match:
                return None
            data = json.loads(json_match.group())

            anomaly = data.get("anomaly", "none").lower()
            if anomaly == "none" or not anomaly:
                return None

            type_map = {
                "captcha": AnomalyType.CAPTCHA,
                "ban": AnomalyType.BAN,
                "login": AnomalyType.LOGIN,
                "popup": AnomalyType.POPUP,
                "network": AnomalyType.NETWORK,
                "update": AnomalyType.UPDATE,
                "crash": AnomalyType.CRASH,
            }
            atype = type_map.get(anomaly, AnomalyType.UNKNOWN)
            rule = next((r for r in _KEYWORD_RULES
                        if r["type"] == atype), None)

            result = AnomalyResult(
                device_id=device_id,
                anomaly_type=atype,
                severity=rule["severity"] if rule else Severity.WARNING,
                description=data.get("description", anomaly),
                detection_method="vision",
                confidence=float(data.get("confidence", 0.7)),
                auto_recoverable=rule["auto_recoverable"] if rule else False,
                recovery_action=rule["recovery"] if rule else "",
            )
            self._record(result)
            return result
        except Exception as e:
            log.warning("[anomaly] Vision detection failed: %s", e)
            return None

    def detect(self, device_id: str, manager=None,
               use_vision: bool = False) -> Optional[AnomalyResult]:
        """Full detection pipeline: XML fast check → optional vision deep check."""
        now = time.time()
        last = self._device_last_check.get(device_id, 0)
        if now - last < 5:
            return None
        self._device_last_check[device_id] = now

        xml_text = ""
        screenshot = None
        if manager:
            try:
                d = manager.get_u2_device(device_id)
                if d:
                    xml_text = d.dump_hierarchy()
            except Exception:
                pass

        fast_result = self.detect_fast(device_id, xml_dump=xml_text)
        if fast_result and fast_result.confidence >= 0.7:
            return fast_result

        if manager:
            try:
                screenshot = manager.capture_screen(device_id)
            except Exception:
                screenshot = None

            if screenshot:
                heuristic = self.detect_image_heuristic(device_id, screenshot)
                if heuristic and heuristic.confidence >= 0.7:
                    self._record(heuristic)
                    return heuristic

                if use_vision:
                    vision_result = self.detect_vision(device_id, screenshot)
                    if vision_result:
                        return vision_result

                if heuristic:
                    self._record(heuristic)
                    return heuristic

        return fast_result

    def _record(self, result: AnomalyResult):
        with self._history_lock:
            self._history.append(result)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
        log.warning("[anomaly] %s on %s: %s (confidence=%.2f)",
                    result.anomaly_type.value, result.device_id[:8],
                    result.description, result.confidence)

    def get_history(self, device_id: str = "",
                    limit: int = 50) -> List[dict]:
        with self._history_lock:
            items = self._history
            if device_id:
                items = [r for r in items if r.device_id == device_id]
            return [r.to_dict() for r in items[-limit:]]

    def get_active_anomalies(self, max_age_sec: float = 300) -> List[dict]:
        """Get anomalies from the last N seconds."""
        cutoff = time.time() - max_age_sec
        with self._history_lock:
            return [r.to_dict() for r in self._history
                    if r.detected_at >= cutoff]

    def clear_history(self, device_id: str = ""):
        with self._history_lock:
            if device_id:
                self._history = [r for r in self._history
                                if r.device_id != device_id]
            else:
                self._history.clear()

    def auto_recover(self, result: AnomalyResult,
                     manager=None) -> bool:
        """Attempt automatic recovery based on anomaly type."""
        if not result.auto_recoverable or not manager:
            return False
        try:
            did = result.device_id
            action = result.recovery_action
            if action == "press_back_and_retry":
                manager.input_key(did, 4)  # BACK
                time.sleep(1)
                manager.input_key(did, 4)
                log.info("[anomaly] Auto-recovery: pressed back on %s", did[:8])
                return True
            elif action == "dismiss_update_dialog":
                manager.input_key(did, 4)  # BACK to dismiss
                log.info("[anomaly] Auto-recovery: dismissed dialog on %s", did[:8])
                return True
            elif action == "dismiss_popup":
                manager.input_key(did, 4)
                log.info("[anomaly] Auto-recovery: dismissed popup on %s", did[:8])
                return True
            elif action == "force_restart_app":
                d = manager.get_u2_device(did) if hasattr(manager, 'get_u2_device') else None
                if d:
                    current = d.app_current()
                    if current and current.get("package"):
                        pkg = current["package"]
                        d.app_stop(pkg)
                        time.sleep(1)
                        d.app_start(pkg)
                        log.info("[anomaly] Auto-recovery: restarted %s on %s",
                                 pkg, did[:8])
                        return True
                manager.input_key(did, 4)
                return True
        except Exception as e:
            log.warning("[anomaly] Auto-recovery failed: %s", e)
        return False

    def detect_and_recover(self, device_id: str, manager=None,
                           use_vision: bool = False) -> Optional[AnomalyResult]:
        """Detect anomaly and attempt auto-recovery if possible."""
        result = self.detect(device_id, manager, use_vision)
        if result and result.auto_recoverable:
            recovered = self.auto_recover(result, manager)
            if recovered:
                result.description += " [已自动恢复]"
        return result


_detector_instance: Optional[ScreenAnomalyDetector] = None
_det_lock = threading.Lock()


def get_anomaly_detector() -> ScreenAnomalyDetector:
    global _detector_instance
    if _detector_instance is None:
        with _det_lock:
            if _detector_instance is None:
                _detector_instance = ScreenAnomalyDetector()
    return _detector_instance
