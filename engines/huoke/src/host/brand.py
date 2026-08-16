# -*- coding: utf-8 -*-
"""品牌单一真相（SSOT，2026-08-14）。

历史上「OpenClaw」这个内部代号被硬编码撒在 ~15 个用户可见处（登录页/控制台/PWA/
壁纸/告警/通知/报告/docs…），且副标题三种口径、版本 1.1.0 与 1.2.0 并存、改名要全仓翻。
本模块把**对外显示身份**收敛成一处：改名/改版号/换母品牌只动这一个文件。

对齐官网 `web117/lib/brand.ts` 的权威身份：
  产品 = 智拓 / ReachX（智连系 Growth，🎯 真机获客，邀请制·私有化交付）
  母品牌 = 无界科技 BOUNDLESS（∞「界」，让沟通，无界）

边界（重要）：
  * 本文件只管**对外显示**（人能看到的名字/话术/版本/前缀）。
  * 内部技术标识符（OPENCLAW_* 环境变量 / openclaw.db / com.openclaw.* 包名 /
    openclaw_* 指标 / 日志 tag）属另一层，见 P3「深度标识符迁移」，不在本文件职责内。
  * 内部代号统一为 `huoke`（与 boundless engines/huoke 一致），仅作技术标识用途。
"""
from __future__ import annotations

import json

# ── 版本单一真相（消掉 login 1.1.0 / dashboard 1.2.0 / FastAPI 1.1.0 三处漂移）──
VERSION = "1.2.0"
COPYRIGHT_YEARS = "2024-2026"

# ── 产品身份（对齐官网 web117/lib/brand.ts reachx）──
NAME_ZH = "智拓"
NAME_EN = "ReachX"
LABEL = "智拓 ReachX"                     # 主显示名（中英合璧）
ALT = "GrowthReach"
FAMILY_ZH = "智连系"
FAMILY_EN = "Growth"
SCENE_ZH = "真机获客"
SCENE_EN = "Lead gen"
SUBTITLE_ZH = "真机集群获客 · 智连系"      # 统一副标题（替代三种旧口径）
SUBTITLE_EN = "Real-device lead-gen · Growth"
TRUST_ZH = "邀请制 · 私有化部署"
TRUST_EN = "Invite-only · Private deployment"
EMOJI = "🎯"

# ── 母品牌（用户选定：智拓 + 无界 BOUNDLESS 双品牌并重）──
MOTHER_ZH = "无界科技 BOUNDLESS"
MOTHER_SHORT = "无界 BOUNDLESS"
MOTHER_MARK = "∞"
MOTHER_TAGLINE_ZH = "让沟通，无界"
MOTHER_TAGLINE_EN = "Communication, Boundless."

# ── 内部代号（技术标识用途，非显示；P3 深度迁移的目标名）──
CODENAME = "huoke"

# ── 设备/壁纸显示（唯一泄漏到手机端的品牌面）──
WALLPAPER_TEXT = LABEL                    # 壁纸上画的字
DEVICE_NAME_PREFIX = "ReachX"             # Android 设备名前缀（ASCII 安全）→ ReachX-04


# ── 派生显示串（各面统一从这里取，禁止再散写）──
def app_title() -> str:
    return f"{LABEL} 控制中心"


def login_title() -> str:
    return f"{LABEL} 登录"


def docs_title() -> str:
    return f"{LABEL} Host Task API"


def footer() -> str:
    """页脚：产品 + 版本 + 母品牌 + 版权。"""
    return f"{LABEL} v{VERSION} · {MOTHER_SHORT} © {COPYRIGHT_YEARS}"


def ai_greeting() -> str:
    return f"你好！我是{LABEL} AI 助手。"


def notify_prefix() -> str:
    """IM/webhook 通知前缀。"""
    return f"[{LABEL}]"


def alert_title(lang: str = "zh") -> str:
    return f"{LABEL} 告警" if lang == "zh" else f"{NAME_EN} Alert"


def report_footer() -> str:
    return f"由 {LABEL} 自动生成"


def device_name(index: int) -> str:
    return f"{DEVICE_NAME_PREFIX}-{index:02d}"


def as_dict() -> dict:
    """给前端 JS 注入用（window.__BRAND），静态 JS 里的品牌串从这里取，避免再硬编码。"""
    return {
        "label": LABEL,
        "nameZh": NAME_ZH,
        "nameEn": NAME_EN,
        "subtitle": SUBTITLE_ZH,
        "family": FAMILY_ZH,
        "mother": MOTHER_SHORT,
        "motherFull": MOTHER_ZH,
        "trust": TRUST_ZH,
        "version": VERSION,
        "footer": footer(),
        "aiGreeting": ai_greeting(),
    }


def inject_script() -> str:
    """返回可直接嵌入 HTML <head> 的脚本，暴露 window.__BRAND 供前端读取。"""
    return f"<script>window.__BRAND={json.dumps(as_dict(), ensure_ascii=False)};</script>"
