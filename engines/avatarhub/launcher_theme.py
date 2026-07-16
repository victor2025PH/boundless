# -*- coding: utf-8 -*-
"""
launcher_theme.py — 桌面启动器「设计系统」单一真相层

从 launcher_qt.py 拆分而来（C 线工程债·渐进式 strangler 重构第一步）。
本模块只承载「与业务无关」的 UI 设计系统：
  · 状态色 / 主题令牌（dark/light）
  · 间距 / 圆角 / 字号语义刻度（设计令牌）
  · UI 内容缩放（UI_SCALE）与字体工厂（uifont / S / sp / rad / tfont）
  · 微交互动效总开关（MOTION）
  · 功能域配色（DOMAIN_COLORS）与 OEM 覆盖
  · QSS 生成（build_style）与状态调色板（_state_palette）

设计原则：本模块**不依赖** launcher_qt / app_config / 设置文件，保持纯净、可单测、零循环依赖。
启动期由 launcher_qt 读取设置后回填本模块的可变全局（UI_SCALE / MOTION / _CURRENT_THEME）。
"""
from PySide6.QtGui import QColor, QFont

# ── 设计令牌单源（2026-07-16 桌面×网页对齐 P0）────────────────────────────────
# static/design-tokens.json 是「桌面 QSS」与「网页 brand.css」共同的取值真相：状态色 /
# 品牌色 / 暗色背景层级 / 圆角。随包分发、离线可读（本模块保持零 app_config 依赖）；
# 文件缺失/损坏时回退下方内置值（＝旧版观感，绝不因缺文件起不来）。
def _load_design_tokens() -> dict:
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    # 冻结态（PyInstaller）__file__ 指向临时解包目录 → 令牌在 exe 旁的 static/；
    # 源码态在本文件旁的 static/。两处按序探测（与 app_config._detect_base 同思路，但本模块保持零依赖）。
    bases = []
    if getattr(_sys, "frozen", False):
        bases.append(_Path(_sys.executable).resolve().parent)
    bases.append(_Path(__file__).resolve().parent)
    for b in bases:
        try:
            p = b / "static" / "design-tokens.json"
            if p.exists():
                d = _json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except Exception:
            continue
    return {}


_DT = _load_design_tokens()
_DT_STATE = _DT.get("state") or {}
_DT_DARK = _DT.get("dark") or {}
_DT_LIGHT = _DT.get("light") or {}
_DT_HERO = (_DT_DARK.get("hero") or [])[:3] + ["#1a2745", "#141d33", "#0f1626"][len(_DT_DARK.get("hero") or []):]
_DT_HERO_L = (_DT_LIGHT.get("hero") or [])[:3] + ["#dbe6ff", "#e9f0ff", "#f4f7ff"][len(_DT_LIGHT.get("hero") or []):]

# 品牌默认强调色（_norm_color 的回退；与 launcher_qt.BRAND_DEFAULTS / brand.css --bd-acc 同源）
DEFAULT_ACCENT = _DT.get("accent", "#4F7AFF")
# 品牌辅助色（蓝→紫渐变的第二停靠点；主 CTA 与 Hero 用，对齐网页 --bd-acc2/--bd-grad）
ACCENT2 = _DT.get("accent2", "#a855f7")


def _rgba(hex_color: str, alpha) -> str:
    """#RRGGBB + alpha → 'rgba(r,g,b,a)'。让半透明底色从同一状态色派生，避免散落硬编码。"""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return f"rgba({r},{g},{b},{alpha})"
    except Exception:
        return f"rgba(124,134,148,{alpha})"


# ── 状态色：单一真相＝design-tokens.json（与网页 brand.css --bd-ok/warn/danger 同值）。
# 绿=就绪 / 黄=加载 / 灰=未启动 / 红=异常。全 UI（徽章 / 能力点 / chip / 状态条 / 资源条）
# 统一引用；2026-07-16 起弃用桌面独立色（#2BB673 系），双端同一套状态语义色。
STATE_HEX = {
    "ok":    _DT_STATE.get("ok", "#2BB673"),
    "warn":  _DT_STATE.get("warn", "#E0A33A"),
    "down":  _DT_STATE.get("down", "#7C8694"),
    "error": _DT_STATE.get("danger", "#E2574A"),
}
C_OK = QColor(STATE_HEX["ok"])
C_PARTIAL = QColor(STATE_HEX["warn"])
C_DOWN = QColor(STATE_HEX["down"])
C_ERROR = QColor(STATE_HEX["error"])

# ── UI 缩放与字号分档 ─────────────────────────────────────────────────────────
# 目标：在「系统缩放偏低的高分屏」和「交付现场杂牌显示器」上都保持舒适字号与比例。
# Qt6 已按系统 DPI 自动缩放点字号/px；此处再叠加一档「内容缩放」，专门补偿
# 物理高分但系统缩放偏低（字偏小）/ 低分小屏（需收紧）的两类场景。可被设置项覆盖。
# 由 launcher_qt._init_ui_scale() 在启动时回填本模块全局。
UI_SCALE = 1.0


def uifont(size, weight: int = -1, family: str = "Microsoft YaHei UI") -> QFont:
    """统一字体工厂：按 UI_SCALE 缩放点字号，让全局字号随屏幕分档放大/收紧。
    与 Web 对齐的可读性下限：缩放后不低于 9pt(≈12px)，避免小屏(UI_SCALE<1)把角标/注释压到 11px 以下。"""
    return QFont(family, max(9, int(round(size * UI_SCALE))), weight)


def S(px: float) -> int:
    """按 UI_SCALE 缩放一个像素尺寸（用于固定高度/最小宽度等，保持整体比例）。"""
    return int(round(px * UI_SCALE))


# ── 设计令牌：间距 / 圆角 / 字号刻度（单一真相，全 UI 引用，营造统一节奏与呼吸感）──
# 间距用 4/8pt 基准刻度；布局间距/边距统一从 sp() 取值，使各区块对齐、留白一致。
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
_DT_R = _DT.get("radius") or {}
RADIUS = {"sm": int(_DT_R.get("sm", 10)), "md": int(_DT_R.get("md", 14)),
          "lg": int(_DT_R.get("lg", 20)), "pill": 999}   # 圆角刻度（对齐网页 --bd-r-*；QSS 用 px 不随缩放）
# 字号语义刻度（pt，单一真相）：覆盖现用的全部字号，按"语义角色"命名，全 UI 引用。
# display=品牌主名 · h1/h2=大标题 · title=区块/能力名 · subtitle=徽章/指标值 ·
# body=正文副标题 · label=小标题/描述 · caption=提示/chip · micro=角标 · tiny=最小注释。
# 可读性下限对齐 Web 的 12px：最小语义档 tiny 提到 10pt(≈13px)，uifont 再兜底 9pt(≈12px)，
# 确保即便小屏 UI_SCALE 收紧后仍不低于 ~12px。
TYPE = {"display": 27, "h1": 22, "h2": 18, "title": 15, "subtitle": 14,
        "body": 13, "label": 12, "caption": 11, "micro": 10, "tiny": 10}

# 微交互动效总开关（设置项 reduce_motion 可关；商务现场可"极致稳重·零动画"）。
# 由 launcher_qt._init_motion() / 设置对话框回填本模块全局。
MOTION = True


def sp(key) -> int:
    """间距令牌 → 随 UI_SCALE 缩放的像素（布局 setSpacing / setContentsMargins 用）。
    传入刻度名（'md'）或裸像素值皆可，未知名回退原值。"""
    v = SPACE.get(key, key) if isinstance(key, str) else key
    return S(v)


def rad(key: str) -> int:
    """圆角令牌（裸 px，QSS 不随缩放）。"""
    return RADIUS.get(key, RADIUS["md"])


def tfont(role: str, weight: int = -1, family: str = "Microsoft YaHei UI") -> QFont:
    """字号令牌字体工厂：按语义角色取字号（单一真相），未知角色回退 body。"""
    return uifont(TYPE.get(role, TYPE["body"]), weight, family)


def _auto_ui_scale(app) -> float:
    """按主屏物理分辨率与系统缩放估算内容缩放档位（夹在 0.9–1.2）。
    思路：系统已做的缩放交给 devicePixelRatio；这里只针对「物理像素高、系统缩放低」
    导致的字偏小做补偿，以及对低分小屏做轻微收紧。EDID 缺失时回退到安全的 1.0。"""
    try:
        scr = app.primaryScreen()
        if scr is None:
            return 1.0
        dpr = float(scr.devicePixelRatio() or 1.0)
        phys_w = int(scr.size().width() * dpr)   # 物理像素宽
        if phys_w >= 3000:        # 4K 级
            base = 1.18
        elif phys_w >= 2400:      # 2.5K
            base = 1.10
        elif phys_w >= 1800:      # 1080p / 1200p
            base = 1.0
        elif phys_w >= 1500:      # 1440x900 等
            base = 0.96
        else:                     # 1366x768 及以下
            base = 0.92
        # 系统已放大时（缩放 ≥150%）回收，避免与 dpr 叠加过大
        if dpr >= 2.0:
            base = min(base, 1.0)
        elif dpr >= 1.5:
            base = min(base, 1.05)
        return max(0.9, min(1.2, base))
    except Exception:
        return 1.0


# ── 主题令牌：白天 / 黑夜两套「单一真相」，全部样式引用，便于换肤与 OEM ──────────────
THEMES = {
    "dark": {
        # 2026-07-16 起：底色/表面/描边/文字对齐网页 brand.css（design-tokens.json 单源），
        # 桌面与网页同一暗色世界；Hero 引入品牌蓝紫渐变（网页 --bd-grad 的克制版）。
        # 层级仍是 canvas < 卡片 < 抬起面；输入域比卡片再暗一档呈内凹。
        "BG": _DT_DARK.get("bg", "#090c14"),          # canvas（网页 --bd-bg）
        "SURF1": _DT_DARK.get("surf1", "#161d2b"),    # 卡片（网页 --bd-surface 预混实色）
        "SURF2": _DT_DARK.get("surf2", "#1f2939"),    # 抬起面（网页 --bd-surface2 预混实色）
        "BORDER": _DT_DARK.get("border", "#2c3647"),  # 描边（网页 --bd-border 预混实色）
        "TXT": _DT_DARK.get("txt", "#eef2f9"), "TXT2": _DT_DARK.get("txt2", "#aab4c6"),
        "HERO_G0": _DT_HERO[0], "HERO_G1": _DT_HERO[1], "HERO_G2": _DT_HERO[2],
        "HERO_BORDER": _DT_DARK.get("hero_border", "#31436a"),
        "INPUT_BG": "#0e1116",  # 输入域：比卡片更暗一档，呈现内凹质感
        "PROGRESS_BG": "#0d1015", "LOG_BG": "#0a0d12", "LOG_TXT": "#c4cee0",
        "GHOST_BG": "#171b23", "GHOST_TXT": "#d4dcec", "GHOST_HOVER": "#20252e",
        "TOOL_BG": "#151a24", "TOOL_TXT": "#e3e9f5", "TOOL_BORDER": "#262c38", "TOOL_HOVER": "#1d232e",
        "DEMO_TXT": "#cbd6ee", "DEMO_BORDER": "#2e3850",
        "DISABLED_BG": "#222732", "DISABLED_TXT": "#6a7079",
        "GUIDE_BG": "rgba(255,255,255,0.035)", "WARN_TXT": _DT_STATE.get("warn", "#E0A33A"),
        "CHIP_BG": "rgba(255,255,255,0.045)", "CHIP_BORDER": _DT_DARK.get("border", "#2c3647"),
        "TRUST_BG": "rgba(255,255,255,0.06)", "TRUST_BORDER": "rgba(255,255,255,0.11)", "TRUST_TXT": "#d6e0f2",
        "AVATAR_BG": _DT_DARK.get("surf2", "#1f2939"),
        "SHADOW": (0, 0, 0, 165),
    },
    "light": {
        # 亮色同样走 design-tokens.json 单源（2026-07-16）；Hero 用品牌蓝→淡紫浅色渐变
        "BG": _DT_LIGHT.get("bg", "#eef1f8"), "SURF1": _DT_LIGHT.get("surf1", "#ffffff"),
        "SURF2": _DT_LIGHT.get("surf2", "#e8edf7"), "BORDER": _DT_LIGHT.get("border", "#d3dcec"),
        "TXT": _DT_LIGHT.get("txt", "#1b2333"), "TXT2": _DT_LIGHT.get("txt2", "#5b6678"),
        "HERO_G0": _DT_HERO_L[0], "HERO_G1": _DT_HERO_L[1], "HERO_G2": _DT_HERO_L[2],
        "HERO_BORDER": _DT_LIGHT.get("hero_border", "#c3d2f3"),
        "INPUT_BG": "#ffffff", "PROGRESS_BG": "#e3e9f4", "LOG_BG": "#f3f5fb", "LOG_TXT": "#2a3445",
        "GHOST_BG": "#e7ecf6", "GHOST_TXT": "#2a3445", "GHOST_HOVER": "#d8e0f0",
        "TOOL_BG": "#eef2fb", "TOOL_TXT": "#1f3a6b", "TOOL_BORDER": "#cdd9f0", "TOOL_HOVER": "#e0e8fa",
        "DEMO_TXT": "#2a3a66", "DEMO_BORDER": "#c3d2f3",
        "DISABLED_BG": "#e3e7ef", "DISABLED_TXT": "#a3abba",
        "GUIDE_BG": "rgba(20,30,60,0.04)", "WARN_TXT": "#b3760a",
        "CHIP_BG": "rgba(20,30,60,0.04)", "CHIP_BORDER": "#d3dcec",
        "TRUST_BG": "rgba(79,122,255,0.10)", "TRUST_BORDER": "rgba(79,122,255,0.24)", "TRUST_TXT": "#2a3a66",
        "AVATAR_BG": "#e8ecf6",
        "SHADOW": (120, 130, 150, 70),
    },
}

# 当前主题（由 launcher_qt 在启动/切换时回填本模块全局）
_CURRENT_THEME = "dark"


def theme_tokens(theme: str = None) -> dict:
    """返回当前（或指定）主题的令牌字典。"""
    return THEMES.get(theme or _CURRENT_THEME, THEMES["dark"])


def _state_palette(state: str, accent: str = "#4F7AFF"):
    """状态视觉的单一真相：state → (前景色 QColor, 半透明底色, 语义图标)。

    全 UI（徽章/能力点/chip/状态条）统一引用，确保「绿=就绪 / 黄=加载 / 蓝=信息 /
    灰=未启动 / 红=异常」语义一致，且不仅靠颜色区分（带图标，兼顾色弱可达性）。"""
    acc = QColor(accent)
    tk = theme_tokens()
    down_bg = tk["CHIP_BG"]            # 未启动底色随主题（暗=白透明、亮=深透明）
    warn_fg = QColor(tk["WARN_TXT"])   # 黄色前景随主题加深，保证亮色背景下可读
    table = {
        "ok":    (C_OK,      _rgba(STATE_HEX["ok"], 0.13),    "✓"),
        "warn":  (warn_fg,   _rgba(STATE_HEX["warn"], 0.13),  "●"),
        "down":  (C_DOWN,    down_bg,                         "○"),
        "info":  (acc, f"rgba({acc.red()},{acc.green()},{acc.blue()},0.13)", "•"),
        "error": (C_ERROR,   _rgba(STATE_HEX["error"], 0.13), "⚠"),
    }
    return table.get(state, table["down"])


def _norm_color(c: str) -> str:
    """品牌色归一为 #RRGGBB。网页用 Tailwind 三元组 'R G B'，这里同时兼容。"""
    c = (c or "").strip()
    if not c:
        return DEFAULT_ACCENT
    if c.startswith("#"):
        return c
    parts = c.replace(",", " ").split()
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "#%02X%02X%02X" % tuple(min(255, int(p)) for p in parts)
    return DEFAULT_ACCENT


def _shade(hex_color: str, factor: float) -> str:
    """按 factor 调亮(>1)/调暗(<1)一个 #RRGGBB，用于 hover/pressed 派生色。"""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        f = lambda v: max(0, min(255, int(v * factor)))
        return "#%02X%02X%02X" % (f(r), f(g), f(b))
    except Exception:
        return hex_color


# 功能域配色：每类能力一个强调色（用于图标瓦片/左侧色条/悬停边/对应按钮），
# 仅用在「点缀位」做寻路与区分，背景仍统一，避免花哨。两套主题通用（饱和色在浅/深底都清晰）。
DOMAIN_COLORS = {
    "直播换脸": "#C56FA8",         # 直播换脸 · 玫（幻颜 FaceX 主色，首屏主推能力）
    "克隆你的声音": "#7E78E0",     # 声音克隆 · 紫（降电光感，更沉稳）
    "听懂客户提问": "#36A6BE",     # 语音识别 · 青（转深青，专业）
    "数字人实时开口": "#B06CC0",   # 口型同步 · 紫玫（次要能力，仍保留配色供服务明细/降级用）
    "直播/客服接入": "#D49A3E",    # 直播接入 · 琥珀（与告警琥珀同族，克制）
}
# OEM 换肤：品牌配置可用语义键覆盖功能域色（也兼容直接用中文能力名作键）
_DOMAIN_KEYMAP = {
    "faceswap": "直播换脸", "voice": "克隆你的声音", "asr": "听懂客户提问",
    "lipsync": "数字人实时开口", "stream": "直播/客服接入",
}
D_ASR = DOMAIN_COLORS["听懂客户提问"]
D_STREAM = DOMAIN_COLORS["直播/客服接入"]


def _apply_brand_domains(cfg: dict):
    """允许 OEM 通过品牌配置覆盖功能域色（domain_colors）。空/无效则保留内置默认。"""
    global D_ASR, D_STREAM
    dc = (cfg or {}).get("domain_colors") or {}
    if isinstance(dc, dict):
        for sem, name in _DOMAIN_KEYMAP.items():
            v = dc.get(sem) or dc.get(name)
            if v:
                DOMAIN_COLORS[name] = _norm_color(str(v))
    D_ASR = DOMAIN_COLORS["听懂客户提问"]
    D_STREAM = DOMAIN_COLORS["直播/客服接入"]


def build_style(accent: str, theme: str = None) -> str:
    """按品牌强调色 + 主题（dark/light）生成 QSS。

    颜色全部取自 THEMES 令牌（单一真相），圆角/字号为统一刻度。
    语义色规则：就绪=绿、加载=黄、未启动=灰、强调/主操作=品牌色。
    """
    a_hi = _shade(accent, 1.12)
    a_lo = _shade(accent, 0.88)
    t = theme_tokens(theme)
    BG, SURF1, SURF2, BORDER = t["BG"], t["SURF1"], t["SURF2"], t["BORDER"]
    TXT, TXT2 = t["TXT"], t["TXT2"]
    OK = STATE_HEX["ok"]
    R_SM, R_MD, R_LG = RADIUS["sm"], RADIUS["md"], RADIUS["lg"]   # 圆角刻度（设计令牌单一真相）
    ok_bg, warn_bg, down_bg = (_rgba(STATE_HEX["ok"], 0.16),
                               _rgba(STATE_HEX["warn"], 0.16),
                               _rgba(STATE_HEX["down"], 0.18))   # 徽章底：从状态色派生
    px = lambda n: int(round(n * UI_SCALE))   # QSS 字号随 UI_SCALE 分档
    return f"""
QMainWindow, QWidget, QDialog {{ background: {BG}; color: {TXT}; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: {px(14)}px; }}
/* 通配 QWidget 背景会让每个 QLabel 都刷一块不透明底色——在卡片/Hero 的浅色面上呈现
   「文字背后压着黑块」（1.0.x 长期存在，2026-07-13 客户截图实锤）。QLabel 一律透明，
   需要底色的徽章/chip 自己 setStyleSheet 覆盖（内联优先级更高，不受影响）。 */
QLabel {{ background: transparent; }}
#Brand, #Header {{ color: {TXT}; }}
#Sub {{ color: {TXT2}; }}
#Summary {{ color: {OK}; font-weight: 600; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {t["INPUT_BG"]}; color: {TXT}; border: 1px solid {BORDER};
    border-radius: {R_SM}px; padding: 8px 10px; font-size: {px(13)}px;
}}
QComboBox QAbstractItemView {{ background: {t["INPUT_BG"]}; color: {TXT}; selection-background-color: {a_lo}; }}
QProgressBar {{
    background: {t["PROGRESS_BG"]}; border: 1px solid {BORDER}; border-radius: 8px;
    text-align: center; color: {TXT}; height: 18px;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 7px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: {R_MD}px; margin-top: 10px; padding-top: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; color: {TXT2}; }}
QCheckBox {{ color: {TXT2}; }}
#RootScroll {{ background: transparent; border: none; }}
#RootScroll > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {a_lo}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
#Card {{ background: {SURF1}; border: 1px solid {BORDER}; border-radius: {R_LG}px; }}
#Hero {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {t["HERO_G0"]}, stop:0.54 {t["HERO_G1"]}, stop:1 {t["HERO_G2"]}); border: 1px solid {t["HERO_BORDER"]}; border-radius: {R_LG}px; }}
#Guide {{ background: {t["GUIDE_BG"]}; border: 1px solid {BORDER}; border-radius: {R_MD}px; }}
#SectionCap {{ color: {TXT2}; font-weight: 800; }}
#Step {{ color: {TXT2}; }}
#CapName {{ color: {TXT}; font-weight: 700; }}
#CapHint {{ color: {TXT2}; }}
#CapEng {{ color: {accent}; }}
#Metric {{ color: {TXT2}; }}
#MetricVal {{ color: {TXT}; font-weight: 700; }}
#StatusBig {{ color: {TXT}; font-weight: 900; font-size: {px(22)}px; }}
#Badge, #BadgeOK, #BadgeWarn, #BadgeDown {{ border-radius: {R_MD}px; padding: 8px 16px; font-weight: 800; font-size: {px(14)}px; }}
#BadgeOK   {{ background: {ok_bg}; color: {OK}; }}
#BadgeWarn {{ background: {warn_bg}; color: {t["WARN_TXT"]}; }}
#BadgeDown {{ background: {down_bg}; color: {TXT2}; }}
QTableWidget {{
    background: {SURF1}; gridline-color: {BORDER}; border: 1px solid {BORDER};
    border-radius: {R_MD}px; selection-background-color: {a_lo}; outline: none; font-size: {px(13)}px;
}}
QHeaderView::section {{
    background: {SURF2}; color: {TXT2}; padding: 9px; border: none;
    border-bottom: 1px solid {BORDER}; font-weight: 700;
}}
QTableWidget::item {{ padding: 8px; border-bottom: 1px solid {BORDER}; }}
QPlainTextEdit {{
    background: {t["LOG_BG"]}; color: {t["LOG_TXT"]}; border: 1px solid {BORDER};
    border-radius: {R_MD}px; padding: 10px; font-family: Consolas, monospace; font-size: {px(12)}px;
}}
QPushButton {{
    color: #ffffff; border: none; border-radius: 12px; padding: 11px 18px;
    font-weight: 700; font-size: {px(14)}px;
}}
QPushButton:disabled {{ background: {t["DISABLED_BG"]}; color: {t["DISABLED_TXT"]}; }}
QPushButton#boot {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {ACCENT2});
    font-size: {px(19)}px; font-weight: 900; padding: 20px 28px; border-radius: 14px;
}}
QPushButton#boot:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {a_hi}, stop:1 {_shade(ACCENT2, 1.12)}); }}
QPushButton#boot:pressed {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {a_lo}, stop:1 {_shade(ACCENT2, 0.88)}); }}
QPushButton#primary {{ background: {accent}; }}
QPushButton#primary:hover {{ background: {a_hi}; }}
QPushButton#demo {{
    background: transparent; color: {t["DEMO_TXT"]}; border: 1px solid {t["DEMO_BORDER"]};
    border-radius: 12px; padding: 8px 14px; font-size: {px(13)}px; font-weight: 700;
}}
QPushButton#demo:hover {{ border-color: {accent}; color: {accent}; }}
QPushButton#accent {{ background: #2f6fd6; }}
QPushButton#accent:hover {{ background: #3a82f0; }}
QPushButton#danger {{ background: transparent; color: #e0503e; border: 1px solid #e0503e; }}
QPushButton#danger:hover {{ background: #c0392b; color: #ffffff; }}
QPushButton#ghost {{ background: {t["GHOST_BG"]}; color: {t["GHOST_TXT"]}; border: 1px solid {BORDER}; }}
QPushButton#ghost:hover {{ background: {t["GHOST_HOVER"]}; border-color: {accent}; }}
QPushButton#tool {{
    background: {t["TOOL_BG"]}; color: {t["TOOL_TXT"]}; border: 1px solid {t["TOOL_BORDER"]};
    border-radius: 12px; padding: 10px 16px; font-size: {px(14)}px; font-weight: 700; text-align: left;
}}
QPushButton#tool:hover {{ background: {t["TOOL_HOVER"]}; border-color: {accent}; }}
/* 域色按钮：继承 tool 外观，左侧 4px 域色条做用途分级（背景保持中性，文字保持高对比可读） */
QPushButton#toolBrand, QPushButton#toolAsr, QPushButton#toolStream {{
    background: {t["TOOL_BG"]}; color: {t["TOOL_TXT"]}; border: 1px solid {t["TOOL_BORDER"]};
    border-radius: 12px; padding: 10px 16px; font-size: {px(14)}px; font-weight: 700; text-align: left;
}}
QPushButton#toolBrand {{ border-left: 4px solid {accent}; }}
QPushButton#toolAsr {{ border-left: 4px solid {D_ASR}; }}
QPushButton#toolStream {{ border-left: 4px solid {D_STREAM}; }}
QPushButton#toolBrand:hover {{ background: {t["TOOL_HOVER"]}; border-color: {accent}; border-left-color: {accent}; }}
QPushButton#toolAsr:hover {{ background: {t["TOOL_HOVER"]}; border-color: {D_ASR}; border-left-color: {D_ASR}; }}
QPushButton#toolStream:hover {{ background: {t["TOOL_HOVER"]}; border-color: {D_STREAM}; border-left-color: {D_STREAM}; }}
QPushButton#toolBrand:focus, QPushButton#toolAsr:focus, QPushButton#toolStream:focus {{ border-color: {a_hi}; }}
QPushButton#link {{ background: transparent; color: {TXT2}; padding: 7px 5px; text-align: left; }}
QPushButton#link:hover {{ color: {a_hi}; }}
QPushButton#capGhost {{ background: transparent; color: {TXT2}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 2px 8px; font-size: {px(12)}px; }}
QPushButton#capGhost:hover {{ color: {a_hi}; border-color: {a_hi}; }}
/* 键盘焦点态：仅改颜色/底色，不增减边框，避免布局抖动；提升可达性 */
QPushButton#boot:focus {{ background: {a_hi}; }}
QPushButton#tool:focus, QPushButton#demo:focus {{ border-color: {a_hi}; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {accent}; }}
/* 「更多运维」下拉菜单：随主题，与卡片同语言（圆角/描边/悬停高亮） */
QPushButton::menu-indicator {{ width: 0px; image: none; }}
QMenu {{ background: {SURF2}; color: {TXT}; border: 1px solid {BORDER}; border-radius: {R_SM}px; padding: 6px; }}
QMenu::item {{ padding: 8px 14px; border-radius: 8px; }}
QMenu::item:selected {{ background: {a_lo}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 10px; }}
"""
