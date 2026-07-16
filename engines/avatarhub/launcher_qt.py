# -*- coding: utf-8 -*-
"""
launcher_qt.py — 桌面启动器（PySide6 品牌化界面）

实时数字人对话系统的图形控制台：一键启停/重启、实时就绪状态、打开控制台、一键体检。
进程管理复用 service_manager.py 的成熟逻辑；UI 跨线程更新一律走 Qt 信号（队列连接，主线程执行），
线程安全且界面不卡。需要 PySide6（隔离在 .venv_launcher）；无 PySide6 时请用 tkinter 版 launcher.py。
"""
import os, sys, threading, json, time
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QLockFile, QStandardPaths, QSize, QByteArray
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit,
    QFrame, QAbstractItemView, QMessageBox, QDialog, QComboBox, QProgressBar,
    QLineEdit, QFileDialog, QCheckBox, QSpinBox, QDoubleSpinBox, QGroupBox,
    QGridLayout, QSizePolicy, QSystemTrayIcon, QMenu, QStackedWidget,
    QGraphicsDropShadowEffect, QScrollArea, QInputDialog,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

import webbrowser, subprocess
try:
    import winreg  # Windows 注册表（开机自启）；非 Windows 环境降级
except Exception:
    winreg = None
import app_config
import service_manager as sm

try:
    import pack_installer  # 首启向导内核（纯标准库）；缺失时向导不可用，主控台仍正常
except Exception:
    pack_installer = None

try:
    import telemetry        # 匿名健康回执（opt-in）；缺失不影响任何功能
except Exception:
    telemetry = None

POLL_MS = 2000


# ── 设计系统（设计令牌 / 主题 / QSS / 缩放 / 动效）已拆分至 launcher_theme.py ──────
# C 线工程债·渐进式重构：本文件保留「业务/编排」，设计系统单一真相归 launcher_theme。
# 可变全局（UI_SCALE / MOTION / _CURRENT_THEME）经 ui.<name> 读写以保证跨模块同步；
# 纯函数与常量（uifont/S/build_style/STATE_HEX/...）直接按名导入即可（其内部读 ui 模块全局）。
import launcher_theme as ui
from launcher_theme import (
    _rgba, STATE_HEX, C_OK, C_PARTIAL, C_DOWN, C_ERROR,
    uifont, S, sp, rad, tfont, SPACE, RADIUS, TYPE,
    THEMES, theme_tokens, _state_palette, _shade, _norm_color,
    DOMAIN_COLORS, _apply_brand_domains, build_style,
)


def _init_motion():
    """启动时按设置项 reduce_motion 决定是否启用微交互动效（回填 launcher_theme）。"""
    try:
        ui.MOTION = not bool(_load_settings().get("reduce_motion", False))
    except Exception:
        ui.MOTION = True
    return ui.MOTION


def _init_ui_scale(app):
    """启动时确定 UI_SCALE：优先用设置项 ui_scale（数字或 'auto'），否则自动分档。
    结果回填 launcher_theme.UI_SCALE，供其字体/尺寸工厂统一引用。"""
    val = "auto"
    try:
        val = _load_settings().get("ui_scale", "auto")
    except Exception:
        pass
    if isinstance(val, (int, float)):
        ui.UI_SCALE = max(0.8, min(1.5, float(val)))
    else:
        ui.UI_SCALE = ui._auto_ui_scale(app)
    return ui.UI_SCALE


# 应用版本（发版时与 installer/AvatarHub.iss 的 AppVersion、assets/version_info.txt 三处同步）。
# footer「版本」显示此值；manifest.json 的 version 是【组件包清单】版本（首启向导/组件升级用），
# 两者是不同流水线——1.0.6 前 footer 误显组件版本(v1.0.1)，客户以为装的是旧程序。
APP_VERSION = "1.1.0"

# ── 产品内自更新（1.0.8 起）─────────────────────────────────────────────
# 数据源 = 下载站 release_manifest.json（与官网下载页同一真相）。安全三闸：
#   HTTPS + manifest Ed25519 验签（release_sign 钉死公钥，拒无签名/坏签名）+ 安装包 sha256 比对。
# 只在打包版(frozen)默认启用——源码树开发机不该被提示「升级」把仓库盖掉；
# AVATARHUB_UPDATE_DEV=1 可在源码树强开（联调用）。
RELEASE_MANIFEST_URL = os.environ.get(
    "AVATARHUB_RELEASE_URL", "https://usdt2026.cc/releases/release_manifest.json")


def _ver_tuple(s: str) -> tuple:
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", str(s or ""))[:3]) or (0,)


def _update_enabled() -> bool:
    return bool(getattr(sys, "frozen", False)) or \
        os.environ.get("AVATARHUB_UPDATE_DEV", "") == "1"


def _fetch_release_manifest(timeout: float = 12.0) -> dict:
    """拉发布清单并验签。返回 {} 表示「不可用/不可信」（呼叫方静默跳过）。"""
    import urllib.request
    req = urllib.request.Request(RELEASE_MANIFEST_URL,
                                 headers={"User-Agent": f"AvatarHub/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        m = json.loads(r.read().decode("utf-8"))
    try:
        import release_sign
        ok, why = release_sign.verify_manifest(m)
        # 自更新会执行下载的 exe = 全链路最高危面。比组件包更严：无签名一律拒
        # （更新器 1.0.8 首发即带签名清单，无历史包袱，直接上最强档）。
        if not (ok and (m.get("sig") or {}).get("value")):
            print(f"[update] 发布清单验签未通过，忽略：{why}")
            return {}
    except Exception as e:
        print(f"[update] 验签模块异常，忽略清单：{e}")
        return {}
    return m


def _rollout_bucket() -> int:
    """按机器指纹稳定分桶 [0,100)：同一台机每次结果一致，灰度放量可复现、不抖动。
    取不到指纹时回退随机桶（宁可偶尔早一拍收到更新，也不要所有无指纹机永远卡在 0 桶）。"""
    try:
        import license as _lic
        import hashlib as _h
        fp = _lic.machine_fingerprint() or ""
        return int(_h.sha256(fp.encode()).hexdigest(), 16) % 100
    except Exception:
        import random
        return random.randint(0, 99)


def _win_build(m: dict) -> dict:
    for b in (m.get("builds") or []):
        if "windows" in str(b.get("os", "")).lower():
            return b or {}
    return {}


def _mk_update(b: dict) -> dict:
    return {"ver": str(b.get("ver")), "url": str(b.get("url")),
            "sha256": str(b.get("sha256")).lower(),
            "bytes": int(b.get("bytes") or 0),
            "size_h": str(b.get("size") or "")}


def check_app_update(ignore_rollout: bool = False) -> dict:
    """比对线上版本（含灰度分桶）。返回 {ver,url,sha256,bytes,size_h} 或 {}。绝不抛异常。
    ignore_rollout=True 用于用户手动点「检查更新」——人主动要，就不该被灰度拦着。"""
    try:
        m = _fetch_release_manifest()
        b = _win_build(m)
        if _ver_tuple(b.get("ver")) > _ver_tuple(APP_VERSION) and b.get("url") and b.get("sha256"):
            rollout = int(m.get("rollout", 100))
            if not ignore_rollout and _rollout_bucket() >= rollout:
                print(f"[update] v{b.get('ver')} 灰度中（rollout={rollout}%），本机暂不提示")
                return {}
            return _mk_update(b)
    except Exception as e:
        print(f"[update] 检查更新失败（忽略）：{e}")
    return {}


def check_rollback() -> dict:
    """维护入口用：线上清单是否登记了「可回滚的次新版」，且比本机低。返回目标或 {}。"""
    try:
        m = _fetch_release_manifest()
        prev = _win_build(m).get("prev") or {}
        if prev.get("url") and prev.get("sha256") \
                and _ver_tuple(prev.get("ver")) < _ver_tuple(APP_VERSION):
            return _mk_update(prev)
    except Exception as e:
        print(f"[update] 回滚检查失败（忽略）：{e}")
    return {}

# ── 品牌：与网页控制台共享单一真相（GET /api/brand → data/brand.json → 内置无界）──────
# website/contact：官网与客服联系方式（Hero 链接行 / 托盘菜单「官网·联系客服」直达）。
# 默认即无界官方；OEM 白标可在 data/brand.json 或「设置→白标」覆盖
BRAND_DEFAULTS = {"name": "无界 BOUNDLESS", "logo": "🎭", "logo_image": "",
                  "color": "#4F7AFF", "product": "数字人实时对话系统",
                  "website": "https://ai26.sbs",
                  "contact": "官网 https://ai26.sbs · TG频道 t.me/hykj7 · 客服群 t.me/hykjz"}

_BRAND_CACHE = None


def resolve_brand(force: bool = False) -> dict:
    """品牌配置解析（与网页一致的单一真相）：
    1) Hub 在线 → GET /api/brand；2) 本地 data/brand.json；3) 内置无界默认。
    结果在进程内缓存，避免每个对话框都做一次网络往返。"""
    global _BRAND_CACHE
    if _BRAND_CACHE is not None and not force:
        return dict(_BRAND_CACHE)
    cfg = {}
    try:
        from urllib.request import urlopen
        import json as _json
        with urlopen("http://127.0.0.1:9000/api/brand", timeout=1.2) as r:
            cfg = (_json.loads(r.read().decode("utf-8")) or {}).get("config") or {}
    except Exception:
        try:
            import json as _json
            p = app_config.BASE / "data" / "brand.json"
            if p.exists():
                cfg = _json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    out = dict(BRAND_DEFAULTS)
    for k in ("name", "logo", "logo_image", "color", "product", "website", "contact"):
        if cfg.get(k):
            out[k] = str(cfg[k])
    out["color"] = _norm_color(out["color"])
    _apply_brand_domains(cfg)   # OEM 可选：覆盖功能域色
    _BRAND_CACHE = dict(out)
    return out


def _brand_logo_pixmap(brand: dict, size: int) -> "QPixmap | None":
    """解析真实品牌 logo 图片：优先 brand['logo_image'] 显式路径，其次约定文件
    data/brand_logo.(png|jpg|svg) / data/logo.png。找不到或加载失败返回 None（回退 emoji）。"""
    candidates = []
    explicit = (brand or {}).get("logo_image", "").strip()
    if explicit:
        p = Path(explicit)
        candidates.append(p if p.is_absolute() else (app_config.BASE / explicit))
    data_dir = app_config.BASE / "data"
    for nm in ("brand_logo.png", "brand_logo.jpg", "brand_logo.jpeg",
               "brand_logo.svg", "logo.png", "logo.jpg"):
        candidates.append(data_dir / nm)
    for c in candidates:
        try:
            if c.exists():
                pm = QPixmap(str(c))
                if not pm.isNull():
                    return pm.scaledToHeight(size, Qt.SmoothTransformation)
        except Exception:
            continue
    return None


def _dlg_brand(parent) -> dict:
    """对话框取品牌：优先复用父窗口已解析的品牌，否则走缓存解析（不再重复联网）。"""
    b = getattr(parent, "brand", None)
    return dict(b) if isinstance(b, dict) else resolve_brand()


def _support_url(brand: dict) -> str:
    """客服直达 URL：从 brand.contact 里抽第一个 t.me 链接（客服群/频道），
    抽不到回退官网。contact 是自由文本（白标可写微信/电话），故用正则宽松匹配。"""
    import re as _re
    txt = (brand or {}).get("contact", "") or ""
    m = _re.search(r"(?:https?://)?t\.me/[A-Za-z0-9_+/]+", txt)
    if m:
        u = m.group(0)
        return u if u.startswith("http") else ("https://" + u)
    return (brand or {}).get("website", "") or BRAND_DEFAULTS["website"]


def _brand_header(brand: dict, subtitle: str) -> QWidget:
    """对话框统一品牌头：logo+名称 + 副标题。"""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    t = QLabel(f"{brand['logo']}  {brand['name']}")
    t.setObjectName("Brand")
    t.setFont(uifont(15, QFont.Bold))
    s = QLabel(subtitle)
    s.setObjectName("Sub")
    s.setFont(uifont(9))
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def _brand_asset(name: str) -> "Path | None":
    """品牌资产文件（assets/brand/*，装机包随包分发；母版在 117:D:\\workspace\\brand-assets）。
    找不到返回 None——所有用点都必须留 emoji/圆点兜底，品牌包缺失时功能不降级。"""
    p = app_config.BASE / "assets" / "brand" / name
    return p if p.exists() else None


def _dot_icon(hex_color: str) -> QIcon:
    """托盘图标：品牌 ∞ 主标 + 右下角状态灯（绿=就绪/黄=启动中/灰=未启动）。
    品牌图缺失时回退纯色圆点（信息不丢，只是没了品牌脸面）。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    mark = _brand_asset("boundless-mark-256.png")
    if mark is not None:
        logo = QPixmap(str(mark))
        if not logo.isNull():
            p.drawPixmap(0, 0, 64, 64, logo)
            # 右下角状态灯：白描边提升在任意托盘底色上的辨识度
            p.setBrush(QColor(hex_color))
            p.setPen(QColor("#ffffff"))
            p.drawEllipse(40, 40, 21, 21)
            p.end()
            return QIcon(pm)
    p.setBrush(QColor(hex_color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pm)


# 能力支柱：把裸服务聚合成用户可理解的「能力」（主成员=核心，其余=同类引擎）
# 产品定位＝直播换脸优先：「直播换脸」置顶为第一张能力卡；数字人实时开口（MuseTalk 口型）
# 降为次要能力（仍可在「高级运维·服务明细」里看到 lipsync 行并单独启停），不再占据首屏能力卡。
PILLARS = [
    ("🎭", "直播换脸", ["faceswap", "faceswap2"]),
    ("🗣", "克隆你的声音", ["fish_tts", "voxcpm"]),
    ("🎙", "听懂客户提问", ["stt", "nemo_stt"]),
    ("📡", "直播/客服接入", ["vcam", "hub"]),
]

# 主链服务（启动器「就绪 / 失败 / 告警」只看这几个）：直播换脸开播必需的最小集。
# 其余（克隆声 fish_tts、语音转文字 Whisper、口型 MuseTalk 等）为增强项——仍会随启动一起拉起，
# 但没起来不拦「开始直播换脸」、不弹红色错误卡、不触发「关键服务掉线」告警
# （真人换脸的口型来自你本人的实时画面，本就不需要 MuseTalk）。
PRIMARY_SERVICES = ("faceswap", "vcam", "hub")

# 能力卡品牌图标（三系七品的产品图标，与官网/安装器同一母版）：
# 幻颜 FaceX=直播换脸 · 幻声 VoiceX=克隆声 · 智聊 ChatX=听懂提问 · 幻影 LiveX=直播接入。
# 文件缺失时 _icon_tile 自动回退 emoji，白标客户换掉 assets/brand 即整体换肤。
CAP_ICON_FILES = {
    "直播换脸": "facex-128.png",
    "克隆你的声音": "voicex-128.png",
    "听懂客户提问": "chatx-128.png",
    "直播/客服接入": "livex-128.png",
}

# 启动等待期轮播提示（随轮询切换，让等待"活"起来且顺带传达卖点）
BOOT_TIPS = [
    "本地运行 · 数据不出机，隐私与合规可控",
    "首次启动需加载大模型，之后再次启动会快很多",
    "就绪后会自动打开直播换脸开播页，无需手动操作",
    "换脸口型跟随你本人的真实画面，无需 AI 合成、零额外延迟",
]

# 能力一句话卖点（填充卡片，避免空心；点击卡片看引擎明细）
CAP_TAGLINES = {
    "直播换脸": "把你的脸实时换成目标形象，直接推流到 OBS / 直播间",
    "克隆你的声音": "用指定音色实时说话，适合主播、客服和 IP 分身",
    "听懂客户提问": "低延迟识别语音内容，让数字人自然接话",
    "直播/客服接入": "可接入虚拟摄像头、OBS、直播间或线下屏幕",
}

# 每个能力的「主操作」=(按钮文案, 直达目标)。点卡片或按钮直接到达「能用」的页面：
#   /ui#clone 声音克隆 · /phone 体验对话 · /ui#profiles 角色形象 · /ui#stream 开播。
# 引擎明细/重启降级为卡片角落的小入口（多数用户不需要看裸服务）。
CAP_ACTIONS = {
    "直播换脸": ("🎭 直播换脸", "/ui#stream"),
    "克隆你的声音": ("➕ 克隆声音", "/ui#clone"),
    "听懂客户提问": ("▶ 体验对话", "/phone"),
    "直播/客服接入": ("📡 去开播", "/ui#stream"),
}

# ── 工作台（全功能入口门户）────────────────────────────────────────────────
# 单一真相 = Hub GET /api/features（与网页首页/命令面板同源，Hub 加功能桌面自动长出入口）；
# Hub 离线时用下面的内置兜底（点击仍会先拉起核心链路再开窗，不会点了没反应）。
_WB_FALLBACK = [
    {"id": "phone", "line": "ChatX 对话", "name": "实时对话", "desc": "免提语音对话数字人", "href": "/phone"},
    {"id": "dashboard", "line": "运营", "name": "数据看板", "desc": "业务与产出数据总览", "href": "/dashboard"},
    {"id": "help", "line": "运营", "name": "使用教程", "desc": "安装 / 使用图文教程", "href": "/help"},
    {"id": "settings", "line": "运营", "name": "设置·白标", "desc": "品牌主色 / 参数配置", "href": "/ui#settings"},
]
# 旧版 Hub 注册表无 ic 字段时按 id 兜底映射（图标名 = static/brand-icons.svg 的 symbol）
_WB_IC_BY_ID = {
    "profiles": "users", "clone": "copy", "voice": "mic", "sing": "music", "batch": "package",
    "phone": "chat", "converse": "flask", "stream": "signal", "interp": "globe",
    "dashboard": "chart", "ops": "probe", "history": "clock", "delivery": "check",
    "logs": "file", "settings": "gear", "help": "book", "verify": "shield", "ask": "help", "setup": "zap",
}
# 行排布：产品线分组压缩为 5 行（短线合并同行），未知新产品线自动补行
_WB_ROW_PLAN = [["常用"], ["VoiceX 音色"], ["ChatX 对话", "LiveX 直播"], ["LingoX 同传", "合规·可信"], ["运营"]]

_SPRITE_SYMBOLS = None


def _sprite_symbol(name: str):
    """解析 static/brand-icons.svg（全站图标单一真相），取 symbol 的 viewBox+路径体。一次解析进程内缓存。"""
    global _SPRITE_SYMBOLS
    if _SPRITE_SYMBOLS is None:
        _SPRITE_SYMBOLS = {}
        try:
            import re as _re
            raw = (app_config.BASE / "static" / "brand-icons.svg").read_text(encoding="utf-8")
            for m in _re.finditer(r'<symbol id="i-([a-z0-9-]+)" viewBox="([^"]+)">(.*?)</symbol>', raw, _re.S):
                _SPRITE_SYMBOLS[m.group(1)] = (m.group(2), m.group(3))
        except Exception:
            _SPRITE_SYMBOLS = {}
    return _SPRITE_SYMBOLS.get(name)


def _sprite_icon(name: str, color: str, px: int = 16, dot: str = None) -> QIcon:
    """把图标库 symbol 渲染成 QIcon（单色线性，颜色可指定）——桌面端与网页端图标像素级同源。
    dot: 可选状态点色（右下角小圆，工作台入口的服务就绪指示：绿=在线 / 灰=未启动）。"""
    got = _sprite_symbol(name)
    if not got:
        return QIcon()
    vb, body = got
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" fill="none" stroke="{color}" '
           f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    try:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        r.render(p)
        if dot:
            rad_ = max(2.4, px * 0.19)
            cx, cy = px - rad_ - 0.5, px - rad_ - 0.5
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme_tokens()["SURF1"]))          # 底环：与卡片同色,把点从线条里"抠"出来
            p.drawEllipse(int(cx - rad_ - 1.2), int(cy - rad_ - 1.2), int((rad_ + 1.2) * 2), int((rad_ + 1.2) * 2))
            p.setBrush(QColor(dot))
            p.drawEllipse(int(cx - rad_), int(cy - rad_), int(rad_ * 2), int(rad_ * 2))
        p.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


class ClickCard(QFrame):
    """可点击的能力卡：点击/回车弹出该能力的引擎明细（在线/延迟/许可/重启）。

    可达性：可被键盘 Tab 聚焦（StrongFocus），回车/空格触发；hover 或聚焦时显示品牌色边框。"""
    clicked = Signal()

    def __init__(self, accent: str):
        super().__init__()
        self._accent = accent
        self._hover = False
        self._focus = False
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)   # 允许键盘 Tab 聚焦

    def _refresh_border(self):
        if self._hover or self._focus:
            self.setStyleSheet(f"#Card {{ border: 1px solid {self._accent}; }}")
        else:
            self.setStyleSheet("")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def _lift(self, up: bool):
        """hover / 聚焦时让卡片"浮起"：动画放大投影模糊并下移投影（暗示抬升），
        不改几何尺寸故不触发布局抖动。MOTION 关闭时直接落到目标值（零动画）。"""
        eff = self.graphicsEffect()
        if eff is None or not hasattr(eff, "blurRadius"):
            return
        if not hasattr(self, "_rest_blur"):   # 首次记录静止态
            self._rest_blur = eff.blurRadius()
            self._rest_yoff = eff.yOffset()
        tb = self._rest_blur + S(14) if up else self._rest_blur
        ty = self._rest_yoff + S(4) if up else self._rest_yoff
        if not ui.MOTION:
            eff.setBlurRadius(tb)
            eff.setYOffset(ty)
            return
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        anims = []
        for prop, end in ((b"blurRadius", tb), (b"yOffset", ty)):
            a = QPropertyAnimation(eff, prop, self)
            a.setDuration(150)
            a.setEndValue(end)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.start()
            anims.append(a)
        self._lift_anims = anims   # 保活，防止动画对象被回收而中断

    def enterEvent(self, e):
        self._hover = True
        self._refresh_border()
        self._lift(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._refresh_border()
        self._lift(self._focus)
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._focus = True
        self._refresh_border()
        self._lift(True)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._focus = False
        self._refresh_border()
        self._lift(self._hover)
        super().focusOutEvent(e)


class CapabilityDialog(QDialog):
    """能力明细：列出该能力下的引擎/服务（在线·延迟·许可·可商用·默认），支持逐个重启。
    只读 + 生命周期操作（不做引擎切换——引擎选择是 Profile 级创作决策，归网页控制台）。"""

    def __init__(self, cap_name: str, members: list, parent=None):
        super().__init__(parent)
        self.cap_name = cap_name
        self.members = members
        self._parent = parent
        self.brand = _dlg_brand(parent)
        self.setWindowTitle(f"{self.brand['name']} · {cap_name}")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(560, 360)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 18, 20, 16)
        self.root.setSpacing(10)
        self.root.addWidget(_brand_header(self.brand, f"{cap_name}　·　引擎明细与重启"))
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.root.addLayout(self.body)
        self.root.addStretch(1)
        bar = QHBoxLayout()
        tip = QLabel("提示：引擎的「选用」在网页控制台按角色配置；此处仅看状态与重启。")
        tip.setObjectName("Sub")
        tip.setWordWrap(True)
        bar.addWidget(tip, 1)
        btn = QPushButton("刷新")
        btn.setObjectName("ghost")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._reload)
        bar.addWidget(btn)
        self.root.addLayout(bar)
        self._reload()

    def _fetch_engines(self) -> dict:
        """{backend_service_key: engine_meta}，外加 __defaults__。失败返回空表。"""
        try:
            from urllib.request import urlopen
            import json as _json
            with urlopen("http://127.0.0.1:9000/api/engines", timeout=1.5) as r:
                data = _json.loads(r.read().decode("utf-8"))
            out = {e.get("backend", ""): e for e in data.get("engines", []) if e.get("backend")}
            out["__defaults__"] = data.get("defaults", {})
            return out
        except Exception:
            return {}

    def _reload(self):
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        emap = self._fetch_engines()
        defaults = emap.get("__defaults__", {})
        status = getattr(self._parent, "_last_status", {}) or {}
        for key in self.members:
            svc = app_config.SERVICES.get(key, {})
            info = status.get(key, {})
            eng = emap.get(key)
            self.body.addWidget(self._engine_row(key, svc, info, eng, defaults))

    def _engine_row(self, key, svc, info, eng, defaults):
        card = QFrame()
        card.setObjectName("Card")
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(10)
        # 状态点
        if info.get("healthy"):
            col, sta = C_OK, "在线"
        elif info.get("running"):
            col, sta = C_PARTIAL, "加载中"
        else:
            col, sta = C_DOWN, "停止"
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {col.name()};")
        h.addWidget(dot)
        # 名称 + 描述/标签
        left = QVBoxLayout()
        left.setSpacing(2)
        disp = (eng.get("name") if eng else None) or svc.get("label", key)
        name_lbl = QLabel(f"{disp}")
        name_lbl.setObjectName("CapName")
        name_lbl.setFont(uifont(11, QFont.Bold))
        left.addWidget(name_lbl)
        tags = [f"{sta}", f":{svc.get('port', '?')}"]
        if eng:
            caps = eng.get("capabilities", {}) or {}
            lic = caps.get("license")
            if lic:
                tags.append(lic)
            if caps.get("commercial"):
                tags.append("可商用")
            ls = eng.get("latency") or {}
            p50 = ls.get("p50_ms") or eng.get("last_latency_ms")
            if p50:
                s = f"p50 {p50}ms"
                if ls.get("p95_ms"):
                    s += f" / p95 {ls['p95_ms']}ms"
                if ls.get("count"):
                    s += f"（{ls['count']}样本）"
                tags.append(s)
            if eng.get("name") and eng.get("name") == defaults.get(eng.get("kind")):
                tags.append("默认")
        sub = QLabel("　·　".join(str(t) for t in tags))
        sub.setObjectName("Sub")
        sub.setFont(uifont(9))
        left.addWidget(sub)
        h.addLayout(left, 1)
        # 重启/启动
        act = QPushButton("重启" if info.get("running") else "启动")
        act.setObjectName("accent" if info.get("running") else "primary")
        act.setCursor(Qt.PointingHandCursor)
        act.clicked.connect(lambda _=False, k=key: self._restart(k))
        h.addWidget(act)
        return card

    def _restart(self, key: str):
        svc = next((s for s in sm.SERVICES if s["name"] == key), None)
        if not svc or self._parent is None:
            return
        running = (getattr(self._parent, "_last_status", {}) or {}).get(key, {}).get("running")
        if running:
            self._parent._run_bg(lambda: (sm.stop_service(key), sm.start_service(svc)),
                                 f"正在重启 {key} …")
        else:
            self._parent._run_bg(lambda: sm.start_service(svc), f"正在启动 {key} …")
        self.accept()


class SettingsDialog(QDialog):
    """运维设置：开机自启（HKCU Run）+ 启动即最小化到托盘。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.brand = _dlg_brand(parent)
        self.setWindowTitle(f"{self.brand['name']} · 设置")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(S(480), S(340))
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
        v.addWidget(_brand_header(self.brand, "运维设置　·　开机自启 / 启动行为"))
        s = _load_settings()
        self.chk_auto = QCheckBox("开机自动启动（随 Windows 登录启动，直接进托盘）")
        self.chk_auto.setChecked(is_autostart_enabled())
        if not getattr(sys, "frozen", False):
            self.chk_auto.setEnabled(False)
            self.chk_auto.setText("开机自动启动（仅安装/打包版可用）")
        self.chk_auto.toggled.connect(self._on_auto)
        v.addWidget(self.chk_auto)
        self.chk_min = QCheckBox("启动时直接最小化到托盘（不弹主窗口）")
        self.chk_min.setChecked(bool(s.get("start_minimized")))
        self.chk_min.toggled.connect(self._on_min)
        v.addWidget(self.chk_min)
        self.chk_customer = QCheckBox("默认使用客户模式（隐藏工程日志、维护、体检和停止按钮）")
        self.chk_customer.setChecked(bool(s.get("customer_mode", True)))
        self.chk_customer.toggled.connect(self._on_customer)
        v.addWidget(self.chk_customer)
        self.chk_motion = QCheckBox("减少动效（关闭卡片浮起 / 状态淡入等微交互，追求极致稳重）")
        self.chk_motion.setChecked(bool(s.get("reduce_motion", False)))
        self.chk_motion.toggled.connect(self._on_reduce_motion)
        v.addWidget(self.chk_motion)
        # 界面缩放：自动按屏幕分档；也可手动指定（适配交付现场各种显示器），重启后生效
        scale_row = QHBoxLayout()
        scale_row.setSpacing(8)
        sl = QLabel("界面缩放")
        scale_row.addWidget(sl)
        self.cmb_scale = QComboBox()
        self._scale_opts = [("自动（按屏幕）", "auto"), ("90%", 0.9), ("100%", 1.0),
                            ("110%", 1.1), ("125%", 1.25), ("150%", 1.5)]
        cur_scale = s.get("ui_scale", "auto")
        for i, (label, val) in enumerate(self._scale_opts):
            self.cmb_scale.addItem(label, val)
            if val == cur_scale:
                self.cmb_scale.setCurrentIndex(i)
        self.cmb_scale.currentIndexChanged.connect(self._on_scale)
        scale_row.addWidget(self.cmb_scale, 1)
        v.addLayout(scale_row)
        self.scale_note = QLabel(f"当前生效缩放：{int(round(ui.UI_SCALE * 100))}%　·　修改后重启程序生效")
        self.scale_note.setObjectName("Sub")
        v.addWidget(self.scale_note)
        note = QLabel("关闭主窗口 = 最小化到托盘，服务继续后台运行；彻底退出请用托盘右键「退出」。")
        note.setObjectName("Sub")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addStretch(1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        btn = QPushButton("关闭")
        btn.setObjectName("ghost")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.accept)
        bar.addWidget(btn)
        v.addLayout(bar)

    def _on_auto(self, on: bool):
        if not set_autostart(on):
            self.chk_auto.blockSignals(True)
            self.chk_auto.setChecked(is_autostart_enabled())
            self.chk_auto.blockSignals(False)
            QMessageBox.warning(self, "开机自启", "设置失败：需安装/打包版，且当前用户有写注册表权限。")

    def _on_min(self, on: bool):
        s = _load_settings()
        s["start_minimized"] = bool(on)
        _save_settings(s)

    def _on_customer(self, on: bool):
        s = _load_settings()
        s["customer_mode"] = bool(on)
        _save_settings(s)
        p = self.parent()
        if p is not None and hasattr(p, "customer_mode"):
            p.customer_mode = bool(on)
            p._apply_mode()

    def _on_reduce_motion(self, on: bool):
        s = _load_settings()
        s["reduce_motion"] = bool(on)
        _save_settings(s)
        ui.MOTION = not bool(on)   # 立即生效，无需重启（回填 launcher_theme 全局）

    def _on_scale(self, idx: int):
        val = self.cmb_scale.itemData(idx)
        s = _load_settings()
        s["ui_scale"] = val
        _save_settings(s)
        eff = ui._auto_ui_scale(QApplication.instance()) if val == "auto" else float(val)
        self.scale_note.setText(
            f"当前生效缩放：{int(round(ui.UI_SCALE * 100))}%　→　重启后：{int(round(eff * 100))}%")


class AppUpdateDialog(QDialog):
    """产品内一键升级：下载(进度/速度/剩余时间) → sha256 校验 → 静默安装 → 自动重启。
    升级期间服务不动（安装器只换控制台文件），装完新控制台自己拉起。"""

    prog = Signal(int, int, float)      # done_bytes, total_bytes, speed_bps
    done = Signal(bool, str)            # ok, err

    def __init__(self, info: dict, brand: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.brand = brand
        self._cancel = False
        self._is_down = _ver_tuple(info["ver"]) < _ver_tuple(APP_VERSION)
        self.setWindowTitle("回滚控制台" if self._is_down else "软件更新")
        self.setStyleSheet(build_style(brand["color"]))
        self.setMinimumWidth(S(480))
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(10)
        head = QLabel((f"回滚到 v{info['ver']}" if self._is_down
                       else f"发现新版本 v{info['ver']}"))
        head.setFont(tfont("title", QFont.Bold))
        v.addWidget(head)
        size_h = str(info.get("size_h") or "").split("（")[0].strip()
        sub = QLabel(f"当前 v{APP_VERSION} → v{info['ver']}"
                     + (f"　·　安装包 {size_h}" if size_h else "")
                     + "\n只更换控制台程序（几十 MB），已下载的 AI 组件与你的角色数据全部保留。")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        v.addWidget(sub)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)
        self.stat = QLabel("点击「立即升级」开始下载。全程约 1–3 分钟。")
        self.stat.setObjectName("Sub")
        v.addWidget(self.stat)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_later = QPushButton("稍后再说")
        self.btn_later.setObjectName("ghost")
        self.btn_later.setCursor(Qt.PointingHandCursor)
        self.btn_later.clicked.connect(self.reject)
        row.addWidget(self.btn_later)
        self.btn_go = QPushButton("⏪ 立即回滚" if self._is_down else "⬆ 立即升级")
        self.btn_go.setObjectName("primary")
        self.btn_go.setCursor(Qt.PointingHandCursor)
        self.btn_go.clicked.connect(self._start)
        row.addWidget(self.btn_go)
        v.addLayout(row)
        self.prog.connect(self._on_prog)
        self.done.connect(self._on_done)

    def _start(self):
        self.btn_go.setEnabled(False)
        self.btn_later.setEnabled(False)
        self.stat.setText("正在下载新版本…")
        threading.Thread(target=self._download, daemon=True).start()

    def _download(self):
        import hashlib
        import tempfile
        import urllib.request
        info = self.info
        dst = Path(tempfile.gettempdir()) / f"AvatarHub-Setup-{info['ver']}.exe"
        try:
            req = urllib.request.Request(
                info["url"], headers={"User-Agent": f"AvatarHub/{APP_VERSION}"})
            h = hashlib.sha256()
            done_b = 0
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=30) as r, dst.open("wb") as f:
                total = int(r.headers.get("Content-Length") or info.get("bytes") or 0)
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done_b += len(chunk)
                    el = max(0.001, time.time() - t0)
                    self.prog.emit(done_b, total, done_b / el)
            if h.hexdigest().lower() != info["sha256"]:
                dst.unlink(missing_ok=True)
                self.done.emit(False, "安装包校验失败（sha256 不符），已删除。请稍后重试或到官网下载。")
                return
            self._setup_path = dst
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, f"下载失败：{e}")

    def _on_prog(self, done_b: int, total: int, bps: float):
        if total > 0:
            self.bar.setValue(min(100, int(done_b * 100 / total)))
            left = max(0, total - done_b) / max(1.0, bps)
            self.stat.setText(f"下载中 {done_b/1048576:.0f}/{total/1048576:.0f} MB"
                              f"　·　{bps/1048576:.1f} MB/s　·　预计还需 {int(left)+1} 秒")
        else:
            self.stat.setText(f"下载中 {done_b/1048576:.0f} MB　·　{bps/1048576:.1f} MB/s")

    def _on_done(self, ok: bool, err: str):
        if not ok:
            self.stat.setText(err)
            self.btn_go.setEnabled(True)
            self.btn_later.setEnabled(True)
            return
        self.bar.setValue(100)
        self.stat.setText("下载完成，正在切换到新版本…（程序将自动重启，无需操作）")
        try:
            self._apply_and_restart()
        except Exception as e:
            self.stat.setText(f"启动安装失败：{e}")
            self.btn_go.setEnabled(True)
            self.btn_later.setEnabled(True)

    def _apply_and_restart(self):
        """写自更新助手脚本并退出：等本进程退出 → 静默安装 → 拉起新版控制台。
        经典 Windows 自替换模式（exe 运行中无法覆盖自身，必须借外部进程）。"""
        import locale
        import tempfile
        # 升级结果回执锚点：新版首启对账（版本变没变）后匿名上报成败——
        # 灰度放量从「凭感觉」变「看升级成功率数据」的依据（1.0.10 起）。
        try:
            (Path(app_config.BASE) / "runtime").mkdir(exist_ok=True)
            (Path(app_config.BASE) / "runtime" / "update_pending.json").write_text(
                json.dumps({"from": APP_VERSION, "to": str(self.info["ver"]),
                            "ts": int(time.time()), "kind": ("rollback" if self._is_down else "update")}),
                encoding="utf-8")
        except Exception:
            pass
        exe = sys.executable   # frozen: 安装目录里的 AvatarHub.exe
        setup = str(self._setup_path)
        logf = str(Path(tempfile.gettempdir()) / "avatarhub_update.log")
        helper = Path(tempfile.gettempdir()) / "avatarhub_selfupdate.cmd"
        helper.write_text(
            "@echo off\r\n"
            f"echo [%date% %time%] self-update to {self.info['ver']} >> \"{logf}\"\r\n"
            "ping -n 4 127.0.0.1 >nul\r\n"                     # 等旧进程退净（约 3s）
            "taskkill /F /IM AvatarHub.exe >nul 2>&1\r\n"      # 兜底：万一还挂着
            "ping -n 2 127.0.0.1 >nul\r\n"
            f"\"{setup}\" /VERYSILENT /SUPPRESSMSGBOXES /FORCECLOSEAPPLICATIONS /NORESTART "
            f">> \"{logf}\" 2>&1\r\n"
            f"echo [%date% %time%] setup rc=%errorlevel% >> \"{logf}\"\r\n"
            f"start \"\" \"{exe}\"\r\n"
            f"del \"{setup}\" >nul 2>&1\r\n"
            "(goto) 2>nul & del \"%~f0\"\r\n",                 # cmd 自删除惯用法
            # 批处理按系统 ANSI 码页解析：zh-CN=GBK / en-US≈ASCII，路径含本地字符时不至于烂码
            encoding=locale.getpreferredencoding(False) or "utf-8", errors="replace")
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=(subprocess.DETACHED_PROCESS
                                        | subprocess.CREATE_NO_WINDOW
                                        | subprocess.CREATE_NEW_PROCESS_GROUP))
        self.accept()
        w = self.parent()
        if w is not None and hasattr(w, "_quit_app"):
            w._quit_app()
        else:
            QApplication.quit()


class Bridge(QObject):
    """后台线程 -> 主线程的信号桥（队列连接，槽在主线程执行）。"""
    status_ready = Signal(object)
    health_ready = Signal(object)
    log = Signal(str)
    op_done = Signal()
    meta_ready = Signal(object)
    avatar_ready = Signal(bytes)
    profiles_ready = Signal(int)   # Hub 可达时回报角色数量（用于「无角色」空态引导）
    device_ready = Signal(object)  # D-5 设备状态（麦/摄像头/CABLE 红绿，来自 /api/device/checkup?quick=1）
    update_ready = Signal(object)  # 产品内自更新：检查到新版本（{}=已最新，仅手动检查时提示）
    features_ready = Signal(object)  # 工作台功能注册表（/api/features；None=拉取失败稍后重试）


class Launcher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.busy = False
        self._polling = False
        self._last_status = {}
        self.bridge = Bridge()
        self.bridge.status_ready.connect(self._apply_status)
        self.bridge.health_ready.connect(self._apply_health)
        self.bridge.log.connect(self._log)
        self.bridge.op_done.connect(lambda: self._set_busy(False, "操作完成。"))
        self.bridge.meta_ready.connect(self._apply_meta)
        self.bridge.avatar_ready.connect(self._apply_avatar)
        self.bridge.profiles_ready.connect(self._apply_profiles)
        self.bridge.device_ready.connect(self._apply_device)
        self.bridge.update_ready.connect(self._apply_update)
        self._update_info = {}          # 检查到的新版本（空=无）
        self._update_manual = False     # 本次检查是否用户手动触发（决定「已最新」要不要弹提示）
        self._device_poll_ts = 0.0   # 设备体检节流：~30s 一次(quick 模式免录音，<1s)
        self._avatar_loaded = False
        self._profile_count = None   # None=未知/未连通；0=无角色（空态）；>0=已有角色
        _s = _load_settings()
        self.customer_mode = bool(_s.get("customer_mode", True))
        ui._CURRENT_THEME = _s.get("theme") if _s.get("theme") in THEMES else "dark"
        self.trust_chips = []
        self._build_ui()

        self._really_quit = False
        self._tray_hinted = False
        self._prev_core_health = {}
        self._alerted_down = set()
        self._alert_ts = {}
        self._setup_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)
        QTimer.singleShot(200, self._tick)
        # 产品内自更新：启动 5s 后后台静默检查一次（不打扰首屏），发现新版给出可点更新入口
        if _update_enabled():
            QTimer.singleShot(5000, lambda: self._check_update_async(manual=False))
        # 升级结果对账：上次自更新留下的锚点 vs 当前实际版本 → 匿名回执成/败（8s 后台，失败无感）
        QTimer.singleShot(8000, self._report_update_result)

        # 自适应布局：窗口高度变化时防抖判定，渐进收起次要区块
        self._resp_busy = False
        self._resp_timer = QTimer(self)
        self._resp_timer.setSingleShot(True)
        self._resp_timer.timeout.connect(self._apply_responsive)
        QTimer.singleShot(120, self._apply_responsive)

        # 「加载中」黄点呼吸动效：仅当有处于加载态的能力点时才重绘（空转极廉价）
        self._pulse_dots = []
        self._pulse_phase = 0.0
        self.pulse_timer = QTimer(self)
        self.pulse_timer.timeout.connect(self._pulse)
        self.pulse_timer.start(90)
        # 启动进度 / 失败提示状态：_boot_attempted 标记"用户已点过启动"，避免空闲态误报
        self._boot_attempted = False
        self._boot_ts = None
        self._boot_poll = 0

    # ── UI ──────────────────────────────────────────────────
    def _build_ui(self):
        self.brand = resolve_brand()
        self._booting = False
        self.setWindowTitle(f"{self.brand['name']} · 启动台")
        self.resize(S(1180), S(760))
        # 放宽最小宽度，让小屏/分屏也能用：低于约 980 时能力卡自动回流为 2×2（见 _apply_responsive）
        self.setMinimumSize(S(720), S(640))
        self.setStyleSheet(build_style(self.brand["color"]))

        central = QWidget()
        # 滚动容器：窗口高度放不下时整页可滚动，任何区块都不会被压扁/裁字（治本）。
        scroll = QScrollArea()
        scroll.setObjectName("RootScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # 极窄时（< ~380px）出现横向滚动而非裁字，作安全网；正常宽度仍铺满不显示横条
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        central.setMinimumWidth(380)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        # 居中最大宽度容器：窄窗时铺满（canvas 伸缩因子远大于两侧），宽屏时定宽居中、两侧留白
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        canvas = QWidget()
        canvas.setMaximumWidth(1240)
        outer.addWidget(canvas, 1000)
        outer.addStretch(1)
        root = QVBoxLayout(canvas)
        root.setContentsMargins(sp("xl"), sp("xl"), sp("xl"), sp("lg"))
        root.setSpacing(sp("md"))
        self._start_ts = time.time()

        # ── 商用 Hero：品牌主张 + 信任标签 + 全局状态 ──
        hero = QFrame()
        hero.setObjectName("Hero")
        # [门户改版] 薄化：品牌区让位给下方「工作台」，高度 160→128、留白收紧（信息不减，只更紧凑）
        hero.setMinimumHeight(S(128))
        hero.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._shadow(hero, blur=34, accent=True)
        hero_l = QVBoxLayout(hero)
        hero_l.setContentsMargins(20, 12, 20, 10)
        hero_l.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(14)
        # 真实品牌 logo 图片优先（data/brand_logo.* 或 brand.logo_image），缺失时回退 emoji 文案
        logo_pm = _brand_logo_pixmap(self.brand, 46)
        if logo_pm is not None:
            logo_lbl = QLabel()
            logo_lbl.setPixmap(logo_pm)
            logo_lbl.setFixedSize(logo_pm.width(), logo_pm.height())
            head.addWidget(logo_lbl, alignment=Qt.AlignVCenter)
            name_text = self.brand["name"]
        else:
            name_text = f"{self.brand['logo']}  {self.brand['name']}"
        titles = QVBoxLayout()
        titles.setSpacing(3)
        t = QLabel(name_text)
        t.setObjectName("Brand")
        t.setFont(tfont("display", QFont.Bold))
        sub = QLabel("实时直播换脸 · 数字人听懂开口、同步表情，可私有化交付到直播/客服场景")
        sub.setObjectName("Sub")
        sub.setFont(tfont("body"))
        titles.addWidget(t)
        titles.addWidget(sub)
        # 官网 / 客服直达（品牌可配置：website + contact 里的 t.me 链接）
        _site = self.brand.get("website") or BRAND_DEFAULTS["website"]
        _supp = _support_url(self.brand)
        _site_disp = _site.replace("https://", "").replace("http://", "").rstrip("/")
        links = QLabel(
            f'<a href="{_site}" style="color:{self.brand["color"]};text-decoration:none">'
            f'🌐 官网 {_site_disp}</a>'
            f'<span style="color:#8a90a8"> &nbsp;·&nbsp; </span>'
            f'<a href="{_supp}" style="color:{self.brand["color"]};text-decoration:none">'
            f'💬 联系客服</a>')
        links.setFont(tfont("body"))
        links.setOpenExternalLinks(True)
        links.setToolTip(self.brand.get("contact", ""))
        titles.addWidget(links)
        head.addLayout(titles)
        head.addStretch(1)
        self.badge = QLabel("检测中…")
        self.badge.setObjectName("BadgeDown")
        self.badge.setFont(tfont("subtitle", QFont.Bold))
        self.badge.setToolTip("系统总状态：绿=核心链路全部就绪 · 黄=正在启动/加载模型 · 灰=未启动")
        head.addWidget(self.badge, alignment=Qt.AlignVCenter)
        hero_l.addLayout(head)

        trust = QHBoxLayout()
        trust.setSpacing(8)
        self.trust_chips = []
        # 只留两条最硬的卖点（本地私有 + 商用授权）；「现场可交付 / 多机 GPU」偏内部话术，删去减噪
        for txt in ("本地运行 · 数据不出机", "商用授权可验"):
            chip = QLabel(txt)
            chip.setObjectName("Step")
            chip.setFont(uifont(10, QFont.Bold))
            self._style_trust_chip(chip)
            self.trust_chips.append(chip)
            trust.addWidget(chip)
        trust.addStretch(1)
        self.btn_theme = self._btn("", "ghost", self._toggle_theme,
                                   "切换白天 / 夜间主题")
        trust.addWidget(self.btn_theme)
        self.btn_mode = self._btn("", "ghost", self._toggle_customer_mode,
                                  "客户模式隐藏工程按钮；开发者模式显示完整运维工具")
        trust.addWidget(self.btn_mode)
        hero_l.addLayout(trust)
        self._sync_theme_btn()
        root.addWidget(hero)

        # ── 驾驶舱状态灯（2026-07-16 门户改版）：四张能力大卡收敛为一行状态 chips。
        # 「动作」职责已整体归工作台（角色库/克隆/对话/开播都在下方网格），能力区只剩
        # 「状态」职责——点 chip 看引擎明细（在线/延迟/重启）。首屏省出约 100px 给工作台。
        # cap_refs 契约 [(dot, hint, present)] 原样保留：_apply_status 聚合刷新 / _pulse 黄点脉冲零改动。
        cap_section = QWidget()
        csl = QVBoxLayout(cap_section)
        csl.setContentsMargins(0, 0, 0, 0)
        csl.setSpacing(sp("xs"))
        csl.addWidget(self._section_label("核心能力 · 点状态灯看引擎明细"))
        caprow = QHBoxLayout()
        caprow.setSpacing(sp("sm"))
        self.cap_refs = []   # [(dot_label, hint_label, members)]
        self.cap_chips = []
        self.cap_eng_btns = []   # 兼容位：⚙ 已并入 chip 点击（保留空列表，_apply_mode 零改动）
        for idx, (icon, name, members) in enumerate(PILLARS):
            present = [m for m in members if m in app_config.SERVICES]
            dcolor = DOMAIN_COLORS.get(name) or self.brand["color"]
            chip = ClickCard(dcolor)
            chip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            hl = QHBoxLayout(chip)
            hl.setContentsMargins(sp("md"), sp("sm"), sp("md"), sp("sm"))
            hl.setSpacing(7)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {C_DOWN.name()};")
            dot.setFont(uifont(10))
            nm = QLabel(name)
            nm.setObjectName("CapName")
            nm.setFont(uifont(11, QFont.Bold))
            hint = QLabel("检测中…")
            hint.setObjectName("CapHint")
            hint.setFont(uifont(10))
            hl.addWidget(dot)
            hl.addWidget(nm)
            hl.addWidget(hint)
            _tag = CAP_TAGLINES.get(name, "")
            chip.setToolTip((f"{name}：{_tag}\n" if _tag else f"{name}\n")
                            + "状态点：绿=就绪 / 黄=加载中 / 灰=未启动　·　点击查看引擎明细（在线/延迟/重启）")
            chip.setAccessibleName(f"{name} 状态灯（回车看引擎明细）")
            chip.clicked.connect(lambda i=idx: self._open_capability(i))
            caprow.addWidget(chip)
            self.cap_refs.append((dot, hint, present))
            self.cap_chips.append(chip)
        caprow.addStretch(1)
        csl.addLayout(caprow)
        root.addWidget(cap_section)

        # ── 资源卡 + 主 CTA ──
        midrow = QHBoxLayout()
        midrow.setSpacing(sp("md"))
        self.customer_card = self._customer_ready_card()
        self.customer_card.setMinimumHeight(S(144))
        self.customer_card.setMaximumHeight(S(204))
        self._shadow(self.customer_card)
        midrow.addWidget(self.customer_card, 1)
        self.res_card = self._resource_card()
        self.res_card.setMinimumHeight(S(144))
        self.res_card.setMaximumHeight(S(204))
        self._shadow(self.res_card)
        midrow.addWidget(self.res_card, 1)
        cta = QFrame()
        cta.setObjectName("Card")
        cta.setMinimumHeight(S(144))
        cta.setMaximumHeight(S(204))
        self._shadow(cta, blur=30, accent=True)
        cta_l = QVBoxLayout(cta)
        cta_l.setContentsMargins(16, 14, 16, 14)
        self.btn_boot = self._btn("▶  开始直播换脸", "boot", self.on_boot,
                                  "自动启动直播换脸链路并打开开播页——日常只需点这一个按钮")
        self.btn_boot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_boot.setMinimumHeight(S(60))
        # 启动进度（默认隐藏，准备期间出现）：细进度条 + 指标行（X/Y · 已用 · 预计），让等待"活"起来
        self.boot_progress = QProgressBar()
        self.boot_progress.setObjectName("bootbar")
        self.boot_progress.setRange(0, 100)
        self.boot_progress.setValue(0)
        self.boot_progress.setTextVisible(False)
        self.boot_progress.setFixedHeight(S(6))
        self.boot_progress.setVisible(False)
        _acc = self.brand["color"]
        self.boot_progress.setStyleSheet(
            f"QProgressBar#bootbar{{background:{theme_tokens()['PROGRESS_BG']};border:none;border-radius:3px;}}"
            f"QProgressBar#bootbar::chunk{{background:{_acc};border-radius:3px;}}")
        self.boot_status = QLabel("")
        self.boot_status.setObjectName("CapEng")
        self.boot_status.setFont(tfont("caption", QFont.Bold))
        self.boot_status.setWordWrap(True)
        self.boot_status.setVisible(False)
        self.cta_hint = QLabel("首次启动需加载模型（约 1–2 分钟），就绪后自动打开直播换脸开播页。")
        self.cta_hint.setObjectName("CapHint")
        self.cta_hint.setWordWrap(True)
        self._cta_hint_default = self.cta_hint.text()
        cta_l.addWidget(self.btn_boot)
        cta_l.addWidget(self.boot_progress)
        cta_l.addWidget(self.boot_status)
        cta_l.addWidget(self.cta_hint)
        # 次级入口：观看一键演示——描边轻量样式 + 不占满整行，弱化于主按钮之下
        demo_row = QHBoxLayout()
        self.btn_demo = self._btn("✨ 观看一键演示 ›", "demo", self.on_demo,
                                  "自动启动核心服务，选择可用角色并打开数字人体验页")
        self.btn_demo.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        demo_row.addWidget(self.btn_demo)
        demo_row.addStretch(1)
        cta_l.addLayout(demo_row)
        cta_l.addStretch(1)
        midrow.addWidget(cta, 1)
        root.addLayout(midrow)

        # ── 工作台（全功能入口门户）：与网页首页共用 /api/features 单一真相，全部以
        #    独立应用窗口打开（无地址栏，像桌面软件）。Hub 离线时先渲染内置兜底，上线后自动补全。
        #    位置紧跟驾驶舱（Hero/能力/CTA）之下——入口是高频区，排在告警与页脚之前。──
        self.work_section = QWidget()
        wsl = QVBoxLayout(self.work_section)
        wsl.setContentsMargins(0, 0, 0, 0)
        wsl.setSpacing(sp("sm"))
        wsl.addWidget(self._section_label("工作台 · 全部功能（独立窗口打开，与网页首页同一张功能地图）"))
        self.work_rows = QVBoxLayout()
        self.work_rows.setSpacing(sp("xs"))
        wsl.addLayout(self.work_rows)
        self._features_loaded = False
        self._wb_btns = []           # [(btn, ic_name)]，主题切换时重染图标
        self._render_workbench(list(_WB_FALLBACK))
        root.addWidget(self.work_section)
        self.bridge.features_ready.connect(self._apply_features)
        QTimer.singleShot(900, self._load_features_async)

        # ── 友好错误卡（默认隐藏；核心服务失败时出现，给出重试 / 看日志，免去翻日志）──
        self.error_card = self._error_card()
        self._shadow(self.error_card)
        root.addWidget(self.error_card)

        # ── 价值 / 信任面板（版本 · 授权 · 在线引擎 · 运行时长）──
        self.val_card = self._value_panel()
        self._shadow(self.val_card)
        root.addWidget(self.val_card)

        # ── 高级（可折叠）：服务明细 + 进阶操作 ──
        self.btn_adv = self._btn("▸  高级运维：服务明细 / 启动全部 / 重启", "link", self._toggle_advanced,
                                 "展开逐个服务的运行明细，以及启动全部 / 重启选中等进阶操作")
        root.addWidget(self.btn_adv)
        self.adv = QWidget()
        adv_l = QVBoxLayout(self.adv)
        adv_l.setContentsMargins(0, 0, 0, 0)
        adv_l.setSpacing(10)
        self.table = self._build_table()
        adv_l.addWidget(self.table)
        adv_bar = QHBoxLayout()
        self.btn_all = self._btn("🚀  启动全部（含扩展）", "ghost", self.on_start_all,
                                 "启动包含扩展引擎的全部服务（显存占用较高）")
        self.btn_restart = self._btn("🔄  重启选中", "accent", self.on_restart_sel,
                                     "重启服务明细中选中的单个服务")
        adv_bar.addWidget(self.btn_all)
        adv_bar.addWidget(self.btn_restart)
        adv_bar.addStretch(1)
        adv_l.addLayout(adv_bar)
        self.adv.setVisible(False)
        root.addWidget(self.adv)

        # ── 启动进度 + 高级日志（默认产品化，详细日志按需展开）──
        self.logbar_widget = QWidget()
        logbar = QHBoxLayout()
        logbar.setContentsMargins(0, 0, 0, 0)
        self.logbar_widget.setLayout(logbar)
        lg = QLabel("启动进度")
        lg.setObjectName("Sub")
        lg.setFont(uifont(12, QFont.Bold))
        logbar.addWidget(lg)
        logbar.addStretch(1)
        self.btn_log_toggle = self._btn("📄  查看详细日志", "link", self._toggle_logs, "展开/收起原始启动日志")
        logbar.addWidget(self.btn_log_toggle)
        logbar.addWidget(self._btn("📂  打开日志目录", "link", self._open_logs_dir, "在文件管理器中打开 logs 目录"))
        logbar.addWidget(self._btn("📑  复制", "link", self._copy_log, "复制全部日志到剪贴板"))
        logbar.addWidget(self._btn("🧹  清空", "link", self._clear_log, "清空当前日志显示"))
        root.addWidget(self.logbar_widget)
        # 运行状态改由四张能力卡的状态点 + 顶部总状态徽标承载，删掉原来重复的「运行状态」时间线。
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMinimumHeight(S(120))
        self.logbox.setVisible(False)
        root.addWidget(self.logbox, 1)   # 展开日志时吸收多余空间；默认隐藏，让首页更像产品界面

        # ── 底栏：常用管理直接露出（设置/授权）；进阶运维收进「更多」菜单，减少拥挤；危险操作右侧隔离 ──
        foot = QHBoxLayout()
        foot.setSpacing(sp("sm"))
        self.btn_settings = self._btn("⚙  设置", "ghost", self.on_settings, "开机自启 / 启动即最小化到托盘等运维设置")
        self.btn_license = self._btn("🔑  授权", "ghost", self.on_license, "查看机器指纹 / 输兑换码在线激活 / 导入授权")
        # 进阶 / 集成商工具统一收进「更多运维」下拉，底栏由 8 个并排按钮收敛为 3 个（仅开发者模式可见）
        self.btn_more = QPushButton("⋯  更多运维  ▾")
        self.btn_more.setObjectName("ghost")
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setToolTip("组件 / 维护 / 验收 / 验收清单 / 环境体检 / 一键体检 等进阶运维")
        more_menu = QMenu(self.btn_more)
        more_menu.addAction("🧩  组件 — 加装 / 修复运行环境与模型", self.on_components)
        more_menu.addAction("🔧  维护 — 发布通道 / 回滚 / STT SLA", self.on_maintenance)
        more_menu.addSeparator()
        more_menu.addAction("✅  一键验收 — 按清单核验并出报告", self.on_acceptance)
        more_menu.addAction("📋  验收清单 — 打开交付文档", self.on_open_checklist)
        more_menu.addSeparator()
        more_menu.addAction("🧪  环境体检 — 检查 / 创建 conda 环境", self.on_provision)
        more_menu.addAction("🩺  一键体检 — 整机健康诊断", self.on_doctor)
        more_menu.addAction("🧰  生成诊断包 — 脱敏日志打包发客服", self.on_diag_pack)
        if _update_enabled():
            more_menu.addAction("⏪  回滚控制台版本 — 新版异常时降回上一版", self.on_rollback)
        self.btn_more.setMenu(more_menu)
        for b in (self.btn_settings, self.btn_license, self.btn_more):
            foot.addWidget(b)
        foot.addStretch(1)
        self.btn_stop = self._btn("⏹ 停止全部", "danger", self.on_stop_all, "停止所有正在运行的服务（释放显存）")
        foot.addWidget(self.btn_stop)
        root.addLayout(foot)
        # 兜底伸缩：窗口比内容高时（如最大化），多余竖向空间统一沉到底部，
        # 内容整体顶对齐，避免空间被分摊到区块之间形成大片空隙（展开日志时 logbox 仍会分走空间）。
        root.addStretch(1)

        self.dev_widgets = [self.res_card, self.btn_adv, self.adv, self.logbar_widget, self.logbox,
                            self.btn_more, self.btn_stop]
        self.customer_widgets = [self.customer_card]
        self._refresh_action_btns()
        self._apply_mode()
        self._log(f"欢迎使用 {self.brand['name']}。默认首页只显示商用状态；详细启动日志可按需展开。")
        QTimer.singleShot(150, self._load_meta_async)
        QTimer.singleShot(400, self._load_avatar_async)

    # ── UI 组件工厂 ──
    def _section_label(self, text: str) -> QLabel:
        """区块小标题：用于明确区分「核心能力（介绍）」与「运行状态（动态）」两排卡片，
        消除两组相似卡片造成的「这是介绍还是状态」的困惑。"""
        lb = QLabel(text)
        lb.setObjectName("SectionCap")
        lb.setFont(tfont("caption", QFont.Bold))
        lb.setContentsMargins(4, 2, 0, 0)
        return lb

    # ── 工作台（全功能入口门户）───────────────────────────────────────────
    def _load_features_async(self):
        """后台拉 Hub 功能注册表（与网页首页同源）。失败回 None，主线程按需重试。"""
        def work():
            data = None
            try:
                from urllib.request import urlopen
                import json as _json
                base = app_config.svc_url("hub")
                with urlopen(base + "/api/features", timeout=2.5) as r:
                    j = _json.loads(r.read().decode("utf-8"))
                if j.get("ok") and isinstance(j.get("features"), list):
                    data = j["features"]
            except Exception:
                data = None
            self.bridge.features_ready.emit(data)
        threading.Thread(target=work, daemon=True).start()

    def _apply_features(self, feats):
        """功能注册表到达：重建工作台网格；拉取失败且尚未成功过 → 15s 后静默重试（Hub 可能还没起）。"""
        if feats:
            self._features_loaded = True
            self._render_workbench(list(feats))
        elif not self._features_loaded:
            QTimer.singleShot(15000, self._load_features_async)

    @staticmethod
    def _clear_layout(lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
            elif it.layout() is not None:
                Launcher._clear_layout(it.layout())

    def _wb_button(self, name, tip, ic, slot, kind="ghost", svc=""):
        b = self._btn(f" {name}", kind, slot, tip)
        b.setFont(uifont(11))
        ico = _sprite_icon(ic or "monitor", theme_tokens()["TXT2"], S(15))
        if not ico.isNull():
            b.setIcon(ico)
            b.setIconSize(QSize(S(15), S(15)))
        b.setProperty("_wb_dot", "")          # 就绪点状态缓存（避免每 2s 轮询重复重绘图标）
        self._wb_btns.append((b, ic or "monitor", svc or ""))
        return b

    def _render_workbench(self, feats: list):
        """按产品线分组渲染工作台入口（压缩为 ~5 行）。所有内部页面 → 独立应用窗口 +
        就绪保障（未起先拉核心链路）；换脸面板/直播同传等本地编排入口固定注入。"""
        self._wb_btns = []
        self._clear_layout(self.work_rows)
        by_line = {}
        order = []
        for f in feats:
            ln = f.get("line") or "其他"
            if ln not in by_line:
                by_line[ln] = []
                order.append(ln)
            by_line[ln].append(f)
        # 「常用」合成行：主入口 控制台 + 本地编排入口（不在注册表里的桌面专属能力）
        self.btn_console = self._wb_button("打开控制台", "打开网页控制台 /ui（完整管理与创作），独立窗口",
                                           "monitor", self.on_open_ui, kind="toolBrand")
        self.btn_faceswap = self._wb_button("换脸面板", "打开换脸面板（扩展能力，未启动会自动拉起 faceswap 服务）",
                                            "users", self.on_open_faceswap)
        self.btn_interp_live = self._wb_button("直播同传", "一键拉起直播同传链路并打开（数字人开口说外语）",
                                               "live", self.on_open_interp_live)
        common = [self.btn_console, self.btn_faceswap, self.btn_interp_live]
        self.btn_interp = None
        rows = [ln for grp in _WB_ROW_PLAN for ln in grp]
        plan = list(_WB_ROW_PLAN) + [[ln] for ln in order if ln not in rows]   # 未知产品线自动补行
        for grp in plan:
            row = QHBoxLayout()
            row.setSpacing(sp("sm"))
            has = False
            for ln in grp:
                items = ([{"_common": True}] if ln == "常用" else by_line.get(ln, []))
                if not items:
                    continue
                lab = QLabel(ln)
                lab.setObjectName("CapEng")
                lab.setFont(uifont(10, QFont.Bold))
                lab.setMinimumWidth(S(88))
                row.addWidget(lab)
                if ln == "常用":
                    for b in common:
                        row.addWidget(b)
                    has = True
                    continue
                for f in items:
                    name = f.get("name") or f.get("id") or "?"
                    tip = (f.get("desc") or "") + ("（专业版）" if f.get("edition") == "pro" else "")
                    ic = f.get("ic") or _WB_IC_BY_ID.get(f.get("id") or "", "")
                    fid, href = f.get("id"), f.get("href") or "/ui"
                    svc = f.get("service") or ""
                    if fid == "interp":   # 同传开独立页(7900)更符合"一个功能一扇窗"
                        b = self._wb_button(name, "打开实时同传(通译 LingoX) 独立窗口，未启动会自动拉起",
                                            ic, self.on_open_interp, svc="interpreter")
                        self.btn_interp = b
                    else:
                        b = self._wb_button(name, tip or name, ic,
                                            lambda _=False, p=href: self._open_hub_page(p), svc=svc)
                    row.addWidget(b)
                row.addSpacing(sp("md"))
                has = True
            if has:
                row.addStretch(1)
                self.work_rows.addLayout(row)
        if self.btn_interp is None:   # 注册表没有同传条目（旧版 Hub）→ 兜底放进常用行
            self.btn_interp = self._wb_button("实时同传", "打开实时同传(通译 LingoX)，未启动会自动拉起",
                                              "globe", self.on_open_interp)
            common_row = self.work_rows.itemAt(0)
            if common_row is not None and common_row.layout() is not None:
                common_row.layout().insertWidget(common_row.layout().count() - 1, self.btn_interp)
        self._refresh_action_btns()

    def _update_wb_dots(self, status: dict):
        """工作台就绪点：注册表带 service 字段的入口，按服务健康态在图标右下角点亮
        绿(在线)/灰(未启动)小点。带状态缓存——没变化不重绘（每 2s 轮询零开销）。"""
        for b, ic, svc in getattr(self, "_wb_btns", []):
            if not svc or svc not in status:
                continue
            state = "ok" if status[svc].get("healthy") else "down"
            try:
                if str(b.property("_wb_dot") or "") == state:
                    continue
                b.setProperty("_wb_dot", state)
                ico = _sprite_icon(ic, theme_tokens()["TXT2"], S(15),
                                   dot=(STATE_HEX["ok"] if state == "ok" else STATE_HEX["down"]))
                if not ico.isNull():
                    b.setIcon(ico)
            except RuntimeError:
                pass

    def _refresh_action_btns(self):
        """工作台重建后刷新「忙碌期需禁用」的按钮清单（旧引用已随重建销毁）。"""
        self._quick_btns = [b for b in (getattr(self, "btn_console", None),
                                        getattr(self, "btn_interp", None),
                                        getattr(self, "btn_interp_live", None)) if b is not None]
        base = [getattr(self, n, None) for n in ("btn_boot", "btn_demo", "btn_all", "btn_restart", "btn_stop")]
        self._action_btns = [b for b in base if b is not None] + self._quick_btns

    def _style_trust_chip(self, chip: QLabel):
        """Hero 信任标签的主题化样式（白天/夜间底色与文字不同）。"""
        t = theme_tokens()
        chip.setStyleSheet(
            f"color:{t['TRUST_TXT']}; background:{t['TRUST_BG']}; "
            f"border:1px solid {t['TRUST_BORDER']}; border-radius:12px; padding:5px 11px;")

    def _sync_theme_btn(self):
        """主题切换按钮文案：显示「将切换到」的目标主题，直观。"""
        if getattr(self, "btn_theme", None) is not None:
            self.btn_theme.setText("☀ 白天" if ui._CURRENT_THEME == "dark" else "☾ 夜间")

    def _toggle_theme(self):
        ui._CURRENT_THEME = "light" if ui._CURRENT_THEME == "dark" else "dark"
        s = _load_settings()
        s["theme"] = ui._CURRENT_THEME
        _save_settings(s)
        self._apply_theme()
        self._log("已切换为%s主题。" % ("夜间" if ui._CURRENT_THEME == "dark" else "白天"))

    def _apply_theme(self):
        """重新应用主题：刷新全局 QSS + 所有「内联样式」控件（信任条/头像/状态色/工作台图标）。"""
        self.setStyleSheet(build_style(self.brand["color"]))
        for chip in getattr(self, "trust_chips", []):
            try:
                self._style_trust_chip(chip)
            except RuntimeError:
                pass
        # 工作台线性图标随主题重染（暗色浅灰/亮色深灰），与网页 currentColor 行为一致
        for b, ic, _svc in getattr(self, "_wb_btns", []):
            try:
                dot = str(b.property("_wb_dot") or "") or None
                ico = _sprite_icon(ic, theme_tokens()["TXT2"], S(15),
                                   dot=(STATE_HEX["ok"] if dot == "ok" else STATE_HEX["down"] if dot == "down" else None))
                if not ico.isNull():
                    b.setIcon(ico)
            except RuntimeError:
                pass
        if not getattr(self, "_avatar_loaded", False):
            mode = "empty" if getattr(self, "_profile_count", None) == 0 else "loading"
            self._avatar_placeholder(mode)
        self._sync_theme_btn()
        # 重跑状态/元信息渲染，让 chip / 能力点 / 状态条等内联颜色立即随主题刷新
        if getattr(self, "_last_status", None):
            self._apply_status(self._last_status)
        if getattr(self, "_last_meta", None):
            self._apply_meta(self._last_meta)

    def _icon_tile(self, glyph: str, size: int = 38, color: str = None,
                   image: str = None) -> QLabel:
        """圆角图标瓦片。优先渲染品牌产品图标（assets/brand/<image>，与官网同一母版），
        缺失时回退 emoji 绘制（QPainter 零依赖）。瓦片底色用功能域色微染，
        既统一了「App 图标」式的视觉容器，又保留能力寻路的色彩线索。"""
        base = color or self.brand["color"]
        size = S(size)   # 瓦片随 UI_SCALE 缩放，保持与文字的比例
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        img_pm = None
        if image:
            src = _brand_asset(image)
            if src is not None:
                img_pm = QPixmap(str(src))
                if img_pm.isNull():
                    img_pm = None
        fill = QColor(base)
        fill.setAlpha(28 if img_pm is not None else 46)
        edge = QColor(base)
        edge.setAlpha(90 if img_pm is not None else 110)
        p.setBrush(fill)
        p.setPen(edge)   # 极细品牌色描边，提升瓦片边界清晰度与精致感
        p.drawRoundedRect(1, 1, size - 2, size - 2, 11, 11)
        if img_pm is not None:
            # 品牌图标本身是彩色 3D 立体件：等比缩放到瓦片 82%，居中摆放
            inner = int(size * 0.82)
            scaled = img_pm.scaled(inner, inner, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)
            x = (size - scaled.width()) // 2
            y = (size - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            p.setFont(QFont("Segoe UI Emoji", int(size * 0.44)))
            p.setPen(QColor("#ffffff"))
            p.drawText(pm.rect(), Qt.AlignCenter, glyph)
        p.end()
        lbl = QLabel()
        lbl.setPixmap(pm)
        lbl.setFixedSize(size, size)
        return lbl

    def _shadow(self, w, blur: int = 18, accent: bool = False):
        """给卡片加柔和投影，营造层次（静态、无动画，开销低）。强调卡用品牌色辉光。"""
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(blur)
        eff.setOffset(0, 3)
        if accent:
            c = QColor(self.brand["color"])
            c.setAlpha(120)
        else:
            c = QColor(*theme_tokens()["SHADOW"])
        eff.setColor(c)
        w.setGraphicsEffect(eff)

    # [门户改版·2026-07-16] _cap_card（88px 大卡）与 _reflow_caps（列回流）随能力区
    # 收敛为单行状态灯而退役：动作职责归工作台网格，状态职责归 chips（构建于 _build_ui）。

    def _mini_bar(self) -> QProgressBar:
        b = QProgressBar()
        b.setRange(0, 100)
        b.setValue(0)
        b.setTextVisible(False)
        b.setFixedHeight(6)
        return b

    def _set_bar(self, key: str, pct, color: str):
        bar = self.res_bars.get(key)
        if bar is None:
            return
        try:
            v = max(0, min(100, int(round(pct))))
        except Exception:
            v = 0
        bar.setValue(v)
        bar.setStyleSheet(
            f"QProgressBar{{background:{theme_tokens()['PROGRESS_BG']};border:none;border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px;}}")

    def _customer_ready_card(self):
        card = QFrame()
        card.setObjectName("Card")
        outer = QHBoxLayout(card)
        outer.setContentsMargins(18, 15, 18, 15)
        outer.setSpacing(14)
        # 左：数字人头像预览位（默认占位剪影，加载到当前角色缩略图后替换为真实头像）
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(S(108), S(108))
        self.avatar_label.setAlignment(Qt.AlignCenter)
        self._avatar_placeholder()
        outer.addWidget(self.avatar_label, alignment=Qt.AlignVCenter)
        # 右：状态标题 + 描述 + chip
        v = QVBoxLayout()
        v.setSpacing(8)
        self.customer_status_title = QLabel("直播换脸准备中")
        self.customer_status_title.setObjectName("StatusBig")
        self.customer_status_title.setWordWrap(True)
        v.addWidget(self.customer_status_title)
        self.customer_status_desc = QLabel("点击右侧主按钮后，系统会自动启动所需服务并打开开播页。")
        self.customer_status_desc.setObjectName("CapHint")
        self.customer_status_desc.setFont(uifont(12))
        self.customer_status_desc.setWordWrap(True)
        v.addWidget(self.customer_status_desc)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.customer_chips = {}
        for key, text in (("run", "体验未开始"), ("license", "授权检测中")):
            chip = QLabel(text)
            chip.setObjectName("Step")
            chip.setFont(uifont(10, QFont.Bold))
            _tk = theme_tokens()
            chip.setStyleSheet(
                f"color:{_tk['TXT2']}; background:{_tk['CHIP_BG']}; "
                f"border:1px solid {_tk['CHIP_BORDER']}; border-radius:12px; padding:5px 10px;")
            chips.addWidget(chip)
            self.customer_chips[key] = chip
        chips.addStretch(1)
        v.addLayout(chips)
        v.addStretch(1)
        outer.addLayout(v, 1)
        return card

    def _set_customer_chip(self, key: str, text: str, state: str = "down"):
        chip = getattr(self, "customer_chips", {}).get(key)
        if chip is None:
            return
        # 统一走状态调色板：颜色+底色+语义图标一致（info=品牌色中性事实，不与就绪绿混淆）
        col, bg, icon = _state_palette(state, self.brand["color"])
        color = col.name()
        chip.setText(f"{icon} {text}")
        chip.setStyleSheet(
            f"color:{color}; background:{bg}; border:1px solid {color}; "
            "border-radius:12px; padding:5px 10px;")

    def _avatar_placeholder(self, mode: str = "loading"):
        """头像占位（两态，矢量绘制，让冷启动首屏即有成品感）：
        - loading：渐变衬底 + 品牌色微光 + 优雅半身剪影（头肩），表示「尚未加载到角色头像 / 准备中」；
        - empty：品牌色圆环「＋」，表示「还没有数字人角色」，引导去创建/导入。"""
        if getattr(self, "avatar_label", None) is None:
            return
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import (QLinearGradient, QRadialGradient, QPainterPath,
                                   QBrush, QPen)
        side = S(108)
        radius = S(18)
        pm = QPixmap(side, side)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        acc = QColor(self.brand["color"])
        # 背景：竖向微渐变（顶部略亮），营造质感而非死板纯色
        base = QColor(theme_tokens()["AVATAR_BG"])
        grad = QLinearGradient(0, 0, 0, side)
        grad.setColorAt(0.0, base.lighter(118))
        grad.setColorAt(1.0, base)
        clip = QPainterPath()
        clip.addRoundedRect(1.0, 1.0, side - 2, side - 2, radius, radius)
        p.fillPath(clip, QBrush(grad))
        p.setClipPath(clip)
        if mode == "empty":
            # 优雅「＋」：品牌色圆环 + 加号，提示「去创建 / 导入数字人」
            r = side * 0.20
            cx = cy = side / 2.0
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(acc, max(2, S(2))))
            p.drawEllipse(QPointF(cx, cy), r, r)
            ext = r * 0.5
            p.drawLine(QPointF(cx - ext, cy), QPointF(cx + ext, cy))
            p.drawLine(QPointF(cx, cy - ext), QPointF(cx, cy + ext))
        else:
            # 品牌色柔光衬底（头部位置），让剪影更有层次
            glow = QRadialGradient(side * 0.5, side * 0.42, side * 0.55)
            g0 = QColor(acc); g0.setAlpha(64)
            g1 = QColor(acc); g1.setAlpha(0)
            glow.setColorAt(0.0, g0)
            glow.setColorAt(1.0, g1)
            p.fillRect(0, 0, side, side, QBrush(glow))
            # 半身剪影：头（圆）+ 肩（大椭圆，底部由裁剪自然收边）
            sil = QColor("#8c97b0")
            p.setPen(Qt.NoPen)
            p.setBrush(sil)
            hd = side * 0.155
            p.drawEllipse(QPointF(side / 2.0, side * 0.40), hd, hd)
            sh = QPainterPath()
            sh.addEllipse(QPointF(side / 2.0, side * 1.04), side * 0.40, side * 0.44)
            p.fillPath(sh, QBrush(sil))
        # 右下角 ∞ 品牌水印（低调出品感；品牌包缺失时静默跳过）
        wm = _brand_asset("boundless-mark-256.png")
        if wm is not None:
            wpm = QPixmap(str(wm))
            if not wpm.isNull():
                wsz = int(side * 0.24)
                p.setOpacity(0.55)
                p.drawPixmap(side - wsz - S(6), side - wsz - S(6), wsz, wsz,
                             wpm.scaled(wsz, wsz, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
                p.setOpacity(1.0)
        p.setClipping(False)
        # 品牌色细描边（与卡片其它圆角统一）
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(acc, 1.4))
        p.drawRoundedRect(1, 1, side - 2, side - 2, radius, radius)
        p.end()
        self.avatar_label.setPixmap(pm)

    def _rounded_pixmap(self, src: "QPixmap", side: int = 108, radius: int = 18) -> "QPixmap":
        """把任意图缩放+居中裁剪为正方形并加圆角，作为数字人头像预览。"""
        side = S(side)   # 与缩放后的头像位保持一致
        radius = S(radius)
        scaled = src.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (scaled.width() - side) // 2)
        y = max(0, (scaled.height() - side) // 2)
        scaled = scaled.copy(x, y, side, side)
        out = QPixmap(side, side)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(0, 0, side, side, radius, radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, scaled)
        p.setClipping(False)
        p.setPen(QColor(self.brand["color"]))
        p.drawRoundedRect(1, 1, side - 2, side - 2, radius, radius)
        p.end()
        return out

    def _load_avatar_async(self):
        """后台拉取 /profiles：回报角色数量（区分「无角色」空态）+ 选当前角色缩略图设为头像。
        Hub 不可达时（异常）不回报，状态保持「未知/加载中」，不会误判为空态。"""
        def work():
            try:
                from urllib.request import urlopen
                import json as _json
                base = app_config.svc_url("hub")
                with urlopen(base + "/profiles", timeout=2.5) as r:
                    d = _json.loads(r.read().decode("utf-8"))
                profiles = d.get("profiles") or []
                self.bridge.profiles_ready.emit(len(profiles))   # Hub 可达 → 回报数量
                active = d.get("active") or ""
                chosen = next((p for p in profiles if p.get("name") == active and p.get("thumbnail")), None)
                if not chosen:
                    chosen = next((p for p in profiles if p.get("thumbnail")), None)
                if not chosen:
                    return
                import base64 as _b64
                raw = chosen["thumbnail"]
                raw = raw.split(",", 1)[-1] if "," in raw else raw
                self.bridge.avatar_ready.emit(_b64.b64decode(raw))
            except Exception:
                pass   # Hub 未连通：不回报数量，保持加载态占位
        threading.Thread(target=work, daemon=True).start()

    def _apply_profiles(self, n: int):
        """收到 Hub 角色数量：0 → 进入「无角色」空态（头像占位变「＋」，状态卡引导去创建/导入）。"""
        self._profile_count = int(n)
        if self._profile_count == 0 and not getattr(self, "_avatar_loaded", False):
            self._avatar_placeholder("empty")
        if getattr(self, "_last_status", None) is not None:
            self._apply_status(self._last_status)   # 立即按空态刷新文案

    def _apply_avatar(self, data: bytes):
        if not data or getattr(self, "avatar_label", None) is None:
            return
        pm = QPixmap()
        if pm.loadFromData(data) and not pm.isNull():
            self.avatar_label.setPixmap(self._rounded_pixmap(pm))
            self._avatar_loaded = True

    def _resource_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 11, 16, 11)
        v.setSpacing(5)
        title = QLabel("本机资源")
        title.setObjectName("Sub")
        v.addWidget(title)
        self.res_labels = {}
        self.res_bars = {}
        rows = [("gpu", "GPU 利用率", "GPU 当前算力占用；空闲时接近 0% 属正常"),
                ("vram", "显存", "已加载模型常驻显存，即使空闲也会占用，属正常现象"),
                ("ram", "内存占用", "系统物理内存占用百分比")]
        for key, lbl, tip in rows:
            hb = QHBoxLayout()
            hb.setSpacing(8)
            k = QLabel(lbl)
            k.setObjectName("Metric")
            k.setFont(uifont(9))
            k.setToolTip(tip)
            val = QLabel("—")
            val.setObjectName("MetricVal")
            val.setFont(uifont(10, QFont.Bold, "Consolas"))
            val.setToolTip(tip)
            hb.addWidget(k)
            hb.addStretch(1)
            hb.addWidget(val)
            v.addLayout(hb)
            bar = self._mini_bar()
            bar.setToolTip(tip)
            v.addWidget(bar)
            self.res_labels[key] = val
            self.res_bars[key] = bar
        # D-5 设备状态行（只读红绿灯：麦 / 摄像头 / CABLE，数据来自 Hub 设备体检 quick 模式）
        dev_row = QHBoxLayout()
        dev_row.setSpacing(8)
        dk = QLabel("设备")
        dk.setObjectName("Metric")
        dk.setFont(uifont(9))
        dk.setToolTip("开播三件套快检（30 秒自动刷新）：\n"
                      "🟢 就绪 · 🟡 有提示 · 🔴 需处理 · ⚪ 未测（如未开播测不了脸占比）\n"
                      "完整体检（含 3 秒录音测噪声底）在控制台开播页「🎛 设备体检」")
        dev_row.addWidget(dk)
        dev_row.addStretch(1)
        self.dev_labels = {}
        for key, lbl in (("mic", "麦"), ("camera", "摄像头"), ("cable", "CABLE")):
            d = QLabel(f"⚪{lbl}")
            d.setObjectName("MetricVal")
            d.setFont(uifont(9))
            dev_row.addWidget(d)
            self.dev_labels[key] = d
        v.addLayout(dev_row)
        v.addStretch(1)
        return card

    def _apply_device(self, d: dict):
        """D-5 设备状态灯：把 /api/device/checkup?quick=1 的逐项 level 映射为红绿灯。"""
        if not hasattr(self, "dev_labels"):
            return
        icon = {"good": "🟢", "warn": "🟡", "bad": "🔴"}
        names = {"mic": "麦", "camera": "摄像头", "cable": "CABLE"}
        items = {i.get("key"): i for i in (d.get("items") or [])} if isinstance(d, dict) else {}
        for key, lab in self.dev_labels.items():
            it = items.get(key)
            if not it or not it.get("measured"):
                lab.setText(f"⚪{names[key]}")
                lab.setToolTip((it or {}).get("detail", "Hub 未启动或暂不可测"))
            else:
                lab.setText(f"{icon.get(it.get('level'), '⚪')}{names[key]}")
                tip = it.get("detail", "")
                if it.get("advice"):
                    tip += "\n→ " + it["advice"]
                lab.setToolTip(tip)

    def _value_panel(self) -> QWidget:
        """价值 / 信任面板：版本 · 授权 · 在线引擎 · 运行时长。空白处转为可信度展示。"""
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumHeight(92)
        h = QHBoxLayout(card)
        h.setContentsMargins(18, 12, 18, 12)
        h.setSpacing(8)
        self.val_labels = {}
        self.val_caption_labels = {}
        blocks = [("version", "版本", "当前安装的产品版本"),
                  ("edition", "授权", "当前授权档位 / 状态"),
                  ("engines", "开播准备", "客户模式显示直播换脸是否就绪；开发者模式显示在线引擎数 / 总数"),
                  ("uptime", "运行时长", "控制台本次已运行时间")]
        for i, (key, cap, tip) in enumerate(blocks):
            blk = QVBoxLayout()
            blk.setSpacing(1)
            val = QLabel("—")
            val.setObjectName("MetricVal")
            val.setFont(uifont(14, QFont.Bold))
            val.setToolTip(tip)
            cl = QLabel(cap)
            cl.setObjectName("Sub")
            cl.setFont(uifont(10))
            blk.addWidget(val)
            blk.addWidget(cl)
            self.val_labels[key] = val
            self.val_caption_labels[key] = cl
            h.addLayout(blk)
            if i < len(blocks) - 1:
                h.addStretch(1)
        return card

    def _error_card(self) -> QWidget:
        """首屏友好错误卡：核心服务未就绪时，用人话说明 + 一键重试 / 看日志，免去翻日志。"""
        card = QFrame()
        card.setObjectName("Card")
        card.setStyleSheet(
            f"#Card{{border:1px solid {C_ERROR.name()};background:{theme_tokens()['SURF1']};}}")
        h = QHBoxLayout(card)
        h.setContentsMargins(16, 13, 16, 13)
        h.setSpacing(sp("md"))
        icon = QLabel("⚠")
        icon.setFont(uifont(20))
        icon.setStyleSheet(f"color:{C_ERROR.name()};")
        h.addWidget(icon, alignment=Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(4)
        self.err_title = QLabel("部分服务未能就绪")
        self.err_title.setObjectName("CapName")
        self.err_title.setFont(tfont("title", QFont.Bold))
        self.err_detail = QLabel("")
        self.err_detail.setObjectName("CapHint")
        self.err_detail.setFont(tfont("caption"))
        self.err_detail.setWordWrap(True)
        v.addWidget(self.err_title)
        v.addWidget(self.err_detail)
        btns = QHBoxLayout()
        btns.setSpacing(sp("sm"))
        self.btn_err_retry = self._btn("🔄  重试启动", "primary", self.on_error_retry,
                                       "重新启动核心链路（会自动补起未就绪的服务）")
        self.btn_err_log = self._btn("📄  查看详细日志", "ghost", self._show_logs_expand,
                                     "展开原始启动日志，定位失败原因")
        # 出错处给人（不只给日志）：一键把问题交给客服，降低卡死在错误上的流失
        self.btn_err_support = self._btn(
            "💬  联系客服", "ghost",
            lambda: webbrowser.open(_support_url(self.brand)),
            "打开官方客服（Telegram），把界面截图发给客服可最快解决")
        self.btn_err_diag = self._btn(
            "🧰  生成诊断包", "ghost", self.on_diag_pack,
            "把脱敏日志与环境信息打包到桌面，发给客服可最快定位（不含任何账号/授权私料）")
        btns.addWidget(self.btn_err_retry)
        btns.addWidget(self.btn_err_log)
        btns.addWidget(self.btn_err_support)
        btns.addWidget(self.btn_err_diag)
        btns.addStretch(1)
        v.addLayout(btns)
        h.addLayout(v, 1)
        card.setVisible(False)
        return card

    def on_error_retry(self):
        """错误卡「重试启动」：标记已尝试并重拉核心链路；进度条随轮询自动接管。"""
        self._boot_attempted = True
        self._boot_ts = None
        if getattr(self, "error_card", None) is not None:
            self.error_card.setVisible(False)
        self._run_bg(lambda: sm.start_all(required_only=True),
                     "正在重试启动核心链路（自动补起未就绪服务）…")

    def _report_update_result(self):
        """自更新对账回执：runtime/update_pending.json（升级前写）vs 当前 APP_VERSION。
        版本等于目标=成功；不等=失败（安装被杀/回滚/中断）。匿名事件走既有崩溃上报
        通道（去重限频、本地先落盘、无网留队），给灰度放量提供成功率数据。"""
        pend_f = Path(app_config.BASE) / "runtime" / "update_pending.json"
        try:
            if not pend_f.exists():
                return
            pend = json.loads(pend_f.read_text(encoding="utf-8"))
            pend_f.unlink(missing_ok=True)
            ok = str(pend.get("to")) == APP_VERSION
            kind = pend.get("kind", "update")
            self._log(("✅ 已成功升级到" if ok else "⚠ 版本切换未生效，当前仍是")
                      + f" v{APP_VERSION}"
                      + ("" if ok else f"（目标 v{pend.get('to')}），可重试或联系客服"))
            def work():
                try:
                    import telemetry_client as _tc
                    # report_error 内部已 flush；再手动 flush 会两个发送线程同读队列 →
                    # 同一事件双发（2026-07-13 ingest 实锤重复行），故不重复调用
                    _tc.report_error(
                        "launcher", exc=None,
                        context=f"{kind} {pend.get('from')}->{pend.get('to')} ok={ok}",
                        kind="update")
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            pass

    def on_diag_pack(self):
        """一键诊断包：后台收集（≈3-10s）→ 直传客服后端换 6 位诊断码（1.0.10 起）；
        直传失败回退老动线（落桌面 + 资源管理器选中 + 手动发文件）。"""
        self._log("🧰 正在生成诊断包（脱敏日志 + 环境信息）…")

        def work():
            try:
                import diag_pack
                p = diag_pack.build_diag_pack(app_version=APP_VERSION)
                self.bridge.log.emit(f"✅ 诊断包已生成：{p}")
                ok, msg = diag_pack.upload_diag_pack(p, app_version=APP_VERSION)
                if ok:
                    self.bridge.log.emit(
                        f"📮 已直传客服，诊断码：{msg} —— 联系客服时报这 6 位码即可，无需发文件。")
                    if self.tray is not None:
                        try:
                            self.tray.showMessage(self.brand["name"],
                                                  f"诊断包已送达客服，诊断码 {msg}",
                                                  QSystemTrayIcon.Information, 8000)
                        except Exception:
                            pass
                else:
                    self.bridge.log.emit(f"（直传未成功：{msg}）请把桌面上的诊断包手动发给客服。")
                    try:
                        subprocess.Popen(["explorer", "/select,", str(p)])
                    except Exception:
                        pass
            except Exception as e:
                self.bridge.log.emit(f"⚠ 诊断包生成失败：{e}（可直接截图联系客服）")
        threading.Thread(target=work, daemon=True).start()

    def _show_logs_expand(self):
        """展开详细日志（错误卡入口）：即便客户模式也直接显示，方便现场排查。"""
        if getattr(self, "logbox", None) is None:
            return
        self.logbox.setVisible(True)
        if getattr(self, "btn_log_toggle", None) is not None:
            self.btn_log_toggle.setText("收起详细日志")

    def _toggle_logs(self):
        show = not self.logbox.isVisible()
        self.logbox.setVisible(show)
        self.btn_log_toggle.setText("收起详细日志" if show else "查看详细日志")

    def _apply_mode(self):
        """客户模式默认隐藏工程/危险操作；开发者模式保留完整运维能力。"""
        customer = bool(getattr(self, "customer_mode", True))
        if getattr(self, "btn_mode", None) is not None:
            self.btn_mode.setText("切换开发者模式" if customer else "切换客户模式")
        for w in getattr(self, "customer_widgets", []):
            try:
                w.setVisible(customer)
            except RuntimeError:
                pass
        for w in getattr(self, "dev_widgets", []):
            try:
                # 高级日志本体在开发者模式下仍默认收起，只显示工具条和「查看详细日志」按钮。
                if w is self.logbox:
                    w.setVisible(False)
                    if getattr(self, "btn_log_toggle", None):
                        self.btn_log_toggle.setText("查看详细日志")
                else:
                    w.setVisible(not customer)
            except RuntimeError:
                pass
        # 能力卡状态点 + 提示两种模式都显示（就绪/加载中/未启动），让四张卡自己承载状态，
        # 不再依赖下方那条重复的「运行状态」条；⚙ 引擎明细入口仅开发者模式可见。
        for dot, hint, _ in getattr(self, "cap_refs", []):
            dot.setVisible(True)
            hint.setVisible(True)
        for b in getattr(self, "cap_eng_btns", []):
            try:
                b.setVisible(not customer)
            except RuntimeError:
                pass
        if getattr(self, "val_caption_labels", None):
            cap = self.val_caption_labels.get("engines")
            if cap is not None:
                cap.setText("体验准备" if customer else "在线引擎")

    def _toggle_customer_mode(self):
        self.customer_mode = not bool(getattr(self, "customer_mode", True))
        s = _load_settings()
        s["customer_mode"] = self.customer_mode
        _save_settings(s)
        self._apply_mode()
        self._apply_responsive()
        self._log("已切换为%s。" % ("客户模式" if self.customer_mode else "开发者模式"))

    def _apply_responsive(self):
        """按窗口高度渐进收起次要区块（先时间线、后价值面板），让核心区尽量一屏可见。
        滚动容器仍是安全网；这里只是减少滚动需求。带 40px 滞回，避免临界抖动。"""
        if getattr(self, "_resp_busy", False):
            return
        self._resp_busy = True
        try:
            h = self.height()
            cust = bool(getattr(self, "customer_mode", True))
            # 阈值随 UI_SCALE 缩放：字号放大后，区块需要更多空间才放得下
            vp_th = S(720 if cust else 800)    # 窗口过矮时收起价值面板（核心区优先一屏可见）

            def decide(w, th):
                if w is None:
                    return
                vis = w.isVisible()
                want = (h >= th - 20) if vis else (h >= th + 20)
                if want != vis:
                    w.setVisible(want)

            decide(getattr(self, "val_card", None), vp_th)

            # [门户改版] 能力大卡已收敛为单行状态灯（无需列回流）；窄屏只收次要信息：
            # ① 状态灯的 hint 小字（点/名仍在，语义不丢） ② Hero 卖点 chip ③ 客户卡 chip。
            w = self.width()

            def vis_for(refs, hide_below):
                if not refs:
                    return
                shown = refs[0].isVisible()
                want = (w >= hide_below - 20) if shown else (w >= hide_below + 20)
                if want != shown:
                    for r in refs:
                        r.setVisible(want)

            vis_for([h for _, h, _ in getattr(self, "cap_refs", [])], S(860))
            vis_for(getattr(self, "trust_chips", []), S(980))
            vis_for(list(getattr(self, "customer_chips", {}).values()), S(900))
        finally:
            self._resp_busy = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        t = getattr(self, "_resp_timer", None)
        if t is not None:
            t.start(80)   # 防抖：连续拖拽只在停下后判定一次

    def _load_meta_async(self):
        """后台一次性取版本/授权（联网或读文件，避免卡 UI）；引擎数/运行时长在轮询里更新。"""
        def work():
            # 版本 = 应用版本（与安装包一致）。组件包清单版本是另一条流水线，
            # 别再拿 manifest.json 的 version 冒充程序版本（曾误导客户以为装了旧版）。
            ver = "v" + APP_VERSION
            edi, tip, upsell = "—", "", False
            try:
                import license as _lic
                st = _lic.load_state(force=True)
                pub = st.to_public()
                if st.status == "trial":
                    # 试用 = 全功能 14 天：把「还剩几天」放到台面上，并引导升级
                    edi = f"试用版 · 全功能 · 剩 {max(0, st.days_left)} 天"
                    tip = ("全功能试用期内所有能力开放。到期前激活正式授权即可无缝继续；"
                           "点击这里打开「授权与激活」，或联系客服升级。")
                    upsell = True
                elif st.status == "expired":
                    edi = "试用已到期 · 点此升级"
                    tip = "14 天全功能试用已结束。点击打开「授权与激活」，输入兑换码或联系客服购买正式授权。"
                    upsell = True
                else:
                    edi = "%s·%s" % (pub.get("edition_label") or st.edition,
                                     pub.get("status_label") or st.status)
                    if 0 <= (st.days_left or -1) <= 7:
                        edi += f" · 剩 {st.days_left} 天"
                        tip = "授权即将到期，请尽快续费以免影响使用。点击打开「授权与激活」。"
                        upsell = True
                    else:
                        tip = "商用授权有效。点击查看授权详情。"
            except Exception:
                edi = "评估版"
            self.bridge.meta_ready.emit({"version": ver, "edition": edi,
                                         "edition_tip": tip, "upsell": upsell})
        threading.Thread(target=work, daemon=True).start()

    def _apply_meta(self, m: dict):
        self._last_meta = dict(m) if isinstance(m, dict) else m
        if "version" in m and self.val_labels.get("version"):
            self.val_labels["version"].setText(m["version"])
        if "edition" in m and self.val_labels.get("edition"):
            lab = self.val_labels["edition"]
            lab.setText(m["edition"])
            if m.get("edition_tip"):
                lab.setToolTip(m["edition_tip"])
            # 授权块可点击 → 打开「授权与激活」（试用/临期时染成品牌色引导升级）
            lab.setCursor(Qt.PointingHandCursor)
            if m.get("upsell"):
                lab.setStyleSheet(f"color:{self.brand['color']};")
            if not getattr(lab, "_click_wired", False):
                lab._click_wired = True
                lab.mousePressEvent = lambda _e: self.on_license()
            cap = self.val_caption_labels.get("edition")
            if cap is not None:
                cap.setText("授权（点击升级）" if m.get("upsell") else "授权")
            ok = ("已授权" in str(m["edition"])) or ("valid" in str(m["edition"]).lower())
            trial_on = "试用版" in str(m["edition"]) and "到期" not in str(m["edition"])
            self._set_customer_chip("license",
                                    "商用授权已验证" if ok
                                    else ("全功能试用中" if trial_on else "评估/待授权"),
                                    "ok" if ok else ("info" if trial_on else "warn"))

    # ── 产品内自更新 ────────────────────────────────────────
    def _check_update_async(self, manual: bool = False):
        """后台检查新版本；manual=True 时「已最新/失败」也给用户反馈，且无视灰度放量。"""
        self._update_manual = manual
        def work():
            self.bridge.update_ready.emit(check_app_update(ignore_rollout=manual) or {})
        threading.Thread(target=work, daemon=True).start()

    def _apply_update(self, info: dict):
        # 回滚分支：on_rollback 复用本信号送回目标（{"_rollback": {...}} 或 {"_rollback": None}）
        if isinstance(info, dict) and "_rollback" in info:
            tgt = info["_rollback"]
            if not tgt:
                QMessageBox.information(self, "回滚", "线上清单未登记可回滚的上一版本。")
                return
            if QMessageBox.question(
                    self, "回滚控制台",
                    f"将把控制台从 v{APP_VERSION} 降回 v{tgt['ver']}。\n"
                    f"仅更换控制台程序，AI 组件与角色数据不受影响。是否继续？"
            ) == QMessageBox.Yes:
                AppUpdateDialog(tgt, self.brand, self).exec()
            return
        if not info:
            if self._update_manual:
                QMessageBox.information(self, "软件更新",
                                        f"当前已是最新版本（v{APP_VERSION}）。")
            return
        self._update_info = info
        # 版本块变成升级入口：加箭头提示 + 品牌色 + 可点击
        lab = self.val_labels.get("version")
        cap = self.val_caption_labels.get("version")
        if lab is not None:
            lab.setText(f"v{APP_VERSION} → v{info['ver']}")
            lab.setStyleSheet(f"color:{self.brand['color']};")
            lab.setCursor(Qt.PointingHandCursor)
            lab.setToolTip("发现新版本，点击一键升级（下载 → 自动安装 → 自动重启，约 1–3 分钟）")
            if not getattr(lab, "_upd_wired", False):
                lab._upd_wired = True
                lab.mousePressEvent = lambda _e: self.on_update()
        if cap is not None:
            cap.setText("版本（点击升级）")
        # 托盘气泡只在自动检查时提示一次（手动检查时用户本来就在对话框流程里）
        if not self._update_manual and self.tray is not None:
            try:
                self.tray.showMessage(self.brand["name"],
                                      f"发现新版本 v{info['ver']}，点击主窗口「版本」处一键升级。",
                                      QSystemTrayIcon.Information, 6000)
            except Exception:
                pass
        if self._update_manual:
            self.on_update()

    def on_update(self):
        info = getattr(self, "_update_info", None)
        if not info:
            self._check_update_async(manual=True)
            return
        AppUpdateDialog(info, self.brand, self).exec()

    def on_rollback(self):
        """回滚控制台到线上登记的上一版本（新版翻车时的安全网，走与升级同一套下载/校验/自换流程）。"""
        def work():
            info = check_rollback()
            self.bridge.log.emit(
                f"回滚目标：v{info['ver']}" if info else "线上清单未登记可回滚的上一版本。")
            self.bridge.update_ready.emit({"_rollback": info} if info else {})
        self._log("正在查询可回滚的上一版本…")
        threading.Thread(target=work, daemon=True).start()

    def _fmt_uptime(self) -> str:
        s = int(time.time() - getattr(self, "_start_ts", time.time()))
        if s < 3600:
            return f"{s // 60}m{s % 60:02d}s"
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"

    def _build_table(self):
        names = list(app_config.SERVICES.keys())
        self.row_of = {n: i for i, n in enumerate(names)}
        table = QTableWidget(len(names), 5)
        table.setHorizontalHeaderLabels(["状态", "服务", "说明", "端口", "类型"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.setFixedHeight(248)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        for n, i in self.row_of.items():
            s = app_config.SERVICES[n]
            self._set_cell_t(table, i, 0, "● 检测中", C_DOWN)
            self._set_cell_t(table, i, 1, n)
            self._set_cell_t(table, i, 2, s.get("label", n))
            self._set_cell_t(table, i, 3, str(s["port"]), align=Qt.AlignCenter)
            self._set_cell_t(table, i, 4, "必需" if n in PRIMARY_SERVICES else "可选", align=Qt.AlignCenter)
        return table

    def _toggle_advanced(self):
        show = not self.adv.isVisible()
        self.adv.setVisible(show)
        self.btn_adv.setText(("▾  高级运维：服务明细 / 启动全部 / 重启 / 同传" if show
                              else "▸  高级运维：服务明细 / 启动全部 / 重启 / 同传"))

    def _open_logs_dir(self):
        try:
            d = app_config.BASE / "logs"
            d.mkdir(exist_ok=True)
            os.startfile(str(d))  # noqa: Windows only
        except Exception as e:
            self._log(f"打开日志目录失败：{e}")

    def _copy_log(self):
        QApplication.clipboard().setText(self.logbox.toPlainText())
        self._log("日志已复制到剪贴板。")

    def _clear_log(self):
        self.logbox.clear()

    def _btn(self, text, kind, slot, tip=""):
        b = QPushButton(text)
        b.setObjectName(kind)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return b

    def _set_cell_t(self, table, row, col, text, color=None, align=Qt.AlignVCenter | Qt.AlignLeft):
        item = table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            table.setItem(row, col, item)
        item.setText(text)
        item.setTextAlignment(align)
        if color is not None:
            item.setForeground(color)

    def _set_cell(self, row, col, text, color=None, align=Qt.AlignVCenter | Qt.AlignLeft):
        self._set_cell_t(self.table, row, col, text, color, align)

    def _log(self, msg: str):
        self.logbox.appendPlainText(msg)

    def _set_busy(self, busy: bool, note: str = ""):
        self.busy = busy
        for b in self._action_btns:
            try:
                b.setEnabled(not busy)
            except RuntimeError:
                pass   # 工作台重建瞬间旧按钮已销毁（清单随 _refresh_action_btns 刷新）
        if note:
            self._log(note)

    # ── 轮询 ────────────────────────────────────────────────
    def _tick(self):
        if self._polling:
            return
        self._polling = True

        def work():
            try:
                st = sm.get_status()
                try:
                    self.bridge.status_ready.emit(st)
                except RuntimeError:
                    return  # 窗口/bridge 已销毁（自检快退）→ 安静退出
            except Exception as e:
                try:
                    self.bridge.log.emit(f"探测异常: {e}")
                except RuntimeError:
                    return
            finally:
                self._polling = False
            # 资源指标（hub /health；hub 未起时静默跳过）
            try:
                from urllib.request import urlopen
                import json as _json
                with urlopen("http://127.0.0.1:9000/health", timeout=1.5) as r:
                    h = _json.loads(r.read().decode("utf-8"))
                # 旧版 hub 的 /health 无显存绝对值 → 回退 /api/backpressure（即时可用，无需重启 hub）
                if not (isinstance(h.get("gpu_mem_total"), (int, float)) and h["gpu_mem_total"] > 0):
                    try:
                        with urlopen("http://127.0.0.1:9000/api/backpressure", timeout=1.5) as r2:
                            h["vram"] = (_json.loads(r2.read().decode("utf-8")) or {}).get("vram", {})
                    except Exception:
                        pass
                self.bridge.health_ready.emit(h)
            except Exception:
                try:
                    self.bridge.health_ready.emit({})
                except Exception:
                    pass  # 窗口已销毁（自检快退）时静默
            # D-5 设备状态灯（quick 模式免录音 <1s；~30s 节流；hub 未起时静默置灰）
            if time.time() - self._device_poll_ts >= 30:
                self._device_poll_ts = time.time()
                try:
                    from urllib.request import urlopen
                    import json as _json
                    with urlopen("http://127.0.0.1:9000/api/device/checkup?quick=1", timeout=3.0) as r:
                        self.bridge.device_ready.emit(_json.loads(r.read().decode("utf-8")))
                except Exception:
                    try:
                        self.bridge.device_ready.emit({})
                    except Exception:
                        pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_status(self, status: dict):
        self._last_status = status
        self._update_wb_dots(status)   # 工作台入口就绪点（绿=服务在线/灰=未启动）
        customer = bool(getattr(self, "customer_mode", True))
        core_total = core_ready = 0
        for n, info in status.items():
            i = self.row_of.get(n)
            if i is None:
                continue
            primary = n in PRIMARY_SERVICES     # 主链＝直播换脸开播必需（faceswap/vcam/hub）
            if primary:
                core_total += 1
            if info["healthy"]:
                txt, col = "● 就绪", C_OK
                if primary:
                    core_ready += 1
            elif info["running"]:
                txt, col = "● 加载中", C_PARTIAL
            else:
                txt, col = "● 停止", C_DOWN
            self._set_cell(i, 0, txt, col)

        # 能力卡片：把服务态聚合到 4 大能力
        any_running = False
        eng_online = eng_total = 0
        self._pulse_dots = []
        for dot, hint, members in self.cap_refs:
            prim = [m for m in members if m in PRIMARY_SERVICES]
            online = [m for m in members if status.get(m, {}).get("healthy")]
            running = [m for m in members if status.get(m, {}).get("running")]
            eng_online += len(online)
            eng_total += len(members)
            if running:
                any_running = True
            # 主链能力（直播换脸/直播接入）：其主链成员全就绪才算「就绪」；
            # 增强能力（克隆声/听懂提问等）无主链成员→主引擎(首个成员)就绪即视为「就绪」，
            # 只作展示、不参与全局就绪判定。
            if prim:
                core_ok = all(status.get(m, {}).get("healthy") for m in prim)
            else:
                key = members[0] if members else None
                core_ok = bool(key and status.get(key, {}).get("healthy"))
            if core_ok:
                col, label = C_OK, "就绪"
            elif running:
                col, label = C_PARTIAL, "加载中"
            else:
                col, label = C_DOWN, "未启动"
            dot.setStyleSheet(f"color: {col.name()};")
            if col is C_PARTIAL:
                self._pulse_dots.append(dot)
            if customer:
                # 短状态词即可（图标=功能、按钮=动作、点+词=状态），确保窄列也和按钮同行不挤压
                hint.setText({"就绪": "已就绪", "加载中": "准备中…",
                              "未启动": "未启动"}.get(label, label))
            else:
                hint.setText(f"{label} · {len(online)}/{len(members)} 在线")

        ready = bool(core_total and core_ready >= core_total)
        # 全局徽章
        if ready:
            self._set_badge("●  已就绪" if customer else "●  系统就绪", "BadgeOK")
        elif core_ready or any_running:
            self._set_badge("●  准备中" if customer else f"●  正在启动 {core_ready}/{core_total}", "BadgeWarn")
        else:
            self._set_badge("○  未开始" if customer else "○  未启动", "BadgeDown")

        # 价值面板 + 操作指导条
        if self.val_labels.get("engines"):
            if customer:
                self.val_labels["engines"].setText("完成" if ready else ("准备中" if any_running else "待开始"))
            else:
                self.val_labels["engines"].setText(f"{eng_online}/{eng_total}")
        if self.val_labels.get("uptime"):
            self.val_labels["uptime"].setText(self._fmt_uptime())
        # 服务键是 "hub"（app_config.SERVICES 单一真相）；旧键 "avatar_hub" 永远查空 →
        # 「打开控制台」常年显示未就绪（1.0.8 修复）
        hub_ok = bool(status.get("hub", {}).get("healthy"))
        self._watch_hub(hub_ok, status)
        # 快捷按钮就绪反馈：按 Hub 状态更新提示语 + 文字前缀（非忙碌时才改 enable，避免与忙碌态打架）
        if getattr(self, "btn_console", None) is not None:
            try:
                if hub_ok:
                    self.btn_console.setText(" 打开控制台")
                    self.btn_console.setToolTip("独立应用窗口打开网页控制台 /ui（完整管理与创作）")
                else:
                    self.btn_console.setText(" 打开控制台（未就绪）")
                    self.btn_console.setToolTip("Hub 未就绪：点击将自动启动核心服务，就绪后自动打开")
            except RuntimeError:
                pass   # 工作台重建瞬间旧按钮已销毁，下一轮轮询自然恢复
        # Hub 就绪后角色列表才可用：头像未加载时按 ~10s 限流重试拉取当前角色头像
        if hub_ok and not getattr(self, "_avatar_loaded", False):
            now = time.time()
            if now - getattr(self, "_avatar_last_try", 0) >= 10:
                self._avatar_last_try = now
                self._load_avatar_async()
        # 空态优先：Hub 已连通但还没有任何数字人角色 → 引导去创建/导入（否则即便服务就绪也无内容可体验）
        empty_state = customer and hub_ok and (getattr(self, "_profile_count", None) == 0)
        if empty_state:
            if getattr(self, "customer_status_title", None) is not None:
                self.customer_status_title.setText("还没有换脸形象")
                self.customer_status_desc.setText("点下方「打开控制台」导入或创建一个换脸形象，即可开始直播换脸。")
            self._set_customer_chip("run", "待创建形象", "warn")
            if not getattr(self, "_avatar_loaded", False):
                self._avatar_placeholder("empty")
        elif ready:
            if getattr(self, "customer_status_title", None) is not None:
                self.customer_status_title.setText("直播换脸已就绪")
                self.customer_status_desc.setText("现在可以开始直播换脸，把画面推到虚拟摄像头 / OBS / 直播间。")
            self._set_customer_chip("run", "开播已就绪", "ok")
        elif core_ready or any_running:
            if getattr(self, "customer_status_title", None) is not None:
                self.customer_status_title.setText("正在准备直播换脸")
                self.customer_status_desc.setText("首次加载模型可能需要 1-2 分钟，准备完成后会自动打开开播页。")
            self._set_customer_chip("run", "正在准备", "warn")
        else:
            if getattr(self, "customer_status_title", None) is not None:
                self.customer_status_title.setText("直播换脸待开始")
                self.customer_status_desc.setText("点击右侧「开始直播换脸」，系统会自动准备所需服务。")
            self._set_customer_chip("run", "等待开始", "down")

        # 失败检测：用户已点过启动，且有主链服务处于"停止"（崩溃/未拉起）→ 友好错误卡。
        # 只看主链（直播换脸开播必需）：口型 MuseTalk / 语音转文字 Whisper 没起来不算失败、不弹红卡。
        failed = [app_config.SERVICES.get(n, {}).get("label", n)
                  for n, info in status.items()
                  if n in PRIMARY_SERVICES
                  and not info.get("healthy") and not info.get("running")]
        self._update_boot_ui(ready, core_ready, core_total, any_running, failed)
        self._check_alerts(status)

    def _watch_hub(self, hub_ok: bool, status: dict):
        """Hub 看护：见过健康 → 之后进程消失就自动拉起（客户机不该需要人来救火）。
        防风暴三闸：连续 2 拍确认（防探测抖动）、两次自愈间隔 ≥3 分钟、每会话最多 3 次；
        用户主动「停止全部」会解除武装（on_stop_all 清 _hub_seen_ok），绝不复活人为停掉的服务。"""
        if hub_ok:
            self._hub_seen_ok = True
            self._hub_down_ticks = 0
            return
        if not getattr(self, "_hub_seen_ok", False) or self.busy:
            return
        info = status.get("hub", {}) or {}
        if info.get("running"):          # 进程还在（启动中/加载慢）→ 不是死亡，别打扰
            self._hub_down_ticks = 0
            return
        self._hub_down_ticks = getattr(self, "_hub_down_ticks", 0) + 1
        if self._hub_down_ticks < 2:
            return
        now = time.time()
        if now - getattr(self, "_hub_heal_ts", 0) < 180 or getattr(self, "_hub_heal_n", 0) >= 3:
            return
        self._hub_heal_ts = now
        self._hub_heal_n = getattr(self, "_hub_heal_n", 0) + 1
        self._hub_down_ticks = 0
        self._log(f"⚕ 检测到核心服务意外退出，正在自动恢复（第 {self._hub_heal_n}/3 次）…")
        if self.tray is not None:
            try:
                self.tray.showMessage(self.brand["name"],
                                      "检测到核心服务意外退出，已自动恢复，无需操作。",
                                      QSystemTrayIcon.Information, 5000)
            except Exception:
                pass
        threading.Thread(target=lambda: sm.start_all(required_only=True),
                         daemon=True).start()

    def _update_boot_ui(self, ready, core_ready, core_total, any_running, failed):
        """启动进度 + 失败错误卡的统一刷新（从 _apply_status 调用）。
        - 准备中（已点启动 / 已有服务在跑且未就绪且无失败）：显示细进度条 + 指标行 + 轮播提示；
        - 出现核心服务"停止"且已尝试启动：弹友好错误卡（重试 / 看日志），并收起进度；
        - 就绪 / 空闲：全部收起，恢复默认说明文案。"""
        attempted = bool(getattr(self, "_boot_attempted", False))
        show_err = attempted and bool(failed) and not ready
        # —— 友好错误卡 ——
        if getattr(self, "error_card", None) is not None:
            if show_err:
                self.err_detail.setText(
                    "未能就绪：" + "、".join(failed) + "。可点「重试启动」，或展开详细日志排查原因。")
                self.error_card.setVisible(True)
            else:
                self.error_card.setVisible(False)
        # —— 启动进度 ——
        preparing = (not ready) and (not show_err) and (attempted or any_running)
        bp = getattr(self, "boot_progress", None)
        bs = getattr(self, "boot_status", None)
        if bp is None or bs is None:
            return
        if preparing:
            now = time.time()
            if not self._boot_ts:
                self._boot_ts = now
                self._boot_poll = 0
            else:
                self._boot_poll += 1
            elapsed = now - self._boot_ts
            frac = (core_ready / core_total) if core_total else 0.0
            # 进度条：有就绪服务→按占比；一个都没就绪→忙碌指示
            if frac > 0:
                if bp.maximum() == 0:
                    bp.setRange(0, 100)
                bp.setValue(int(round(frac * 100)))
            else:
                bp.setRange(0, 0)
            # 正常启动约 1–2 分钟；超过 LONG_S 视为"偏久"（多为首次下载模型/服务卡在加载）。
            # 此时不再按 elapsed/frac 线性外推——卡在 2/5 很久会推出"预计还需 9 小时"这种吓人且无意义的数字。
            LONG_S = 300
            if elapsed >= LONG_S:
                mins = int(elapsed // 60)
                span = f"约 {mins} 分钟" if mins <= 30 else "超过 30 分钟"
                bs.setText(f"核心服务仍在启动（已{span}，通常 1–2 分钟）。"
                           "请保持网络畅通稍候；如长时间无进展，可展开详细日志或联系客服。")
            else:
                el = int(elapsed)
                el_str = f"{el // 60}m{el % 60:02d}s" if el >= 60 else f"{el}s"
                if frac <= 0:
                    tail = " · 首次加载模型较慢，请稍候"
                else:
                    # 线性外推仅作粗估，且必须落在可信区间内（≤LONG_S）才显示，否则宁可不显示数字
                    remain = elapsed / frac - elapsed if frac < 1 else 0.0
                    tail = f" · 预计还需 {fmt_eta(remain)}" if 0 < remain <= LONG_S else ""
                bs.setText(f"正在准备核心服务 {core_ready}/{core_total or '—'} · 已用 {el_str}{tail}")
            tip = BOOT_TIPS[(self._boot_poll // 2) % len(BOOT_TIPS)]
            if getattr(self, "cta_hint", None) is not None:
                self.cta_hint.setText("💡 " + tip)
            bp.setVisible(True)
            bs.setVisible(True)
        else:
            self._boot_ts = None
            if bp.maximum() == 0:
                bp.setRange(0, 100)
            bp.setVisible(False)
            bs.setVisible(False)
            if getattr(self, "cta_hint", None) is not None and getattr(self, "_cta_hint_default", None):
                self.cta_hint.setText(self._cta_hint_default)
        if ready:
            self._boot_attempted = False   # 就绪后清旗，避免之后手动停服误判为"准备中"

    def _check_alerts(self, status: dict):
        """核心服务掉线/恢复的边沿告警（弹托盘通知）。仅在「曾就绪→掉线」时报警，
        避免启动期/手动操作期的噪声；恢复仅在曾报警过的服务回到在线时提示。
        档位感知：运行环境未安装的服务不告警——Lite 机上 8090 可能跑着别的口型进程
        （或根本没有），它的死活不是本产品的事，弹「口型同步 已离线」纯属骚扰
        （2026-07-13 在 198 实锤：生产 standby 每崩一次，客户端就弹一次红窗）。"""
        if getattr(self, "tray", None) is None:
            return

        def _alertable(n: str) -> bool:
            if n not in PRIMARY_SERVICES:   # 只对主链（直播换脸开播必需）掉线告警
                return False
            meta = app_config.SERVICES.get(n) or {}
            env = meta.get("env") or ""
            try:
                return (not env) or app_config.env_installed(env)
            except Exception:
                return True   # 判定设施自身出错时宁可保留告警

        cur = {n: bool(info.get("healthy"))
               for n, info in status.items() if _alertable(n)}
        if self.busy:                       # 用户正在启停 → 不告警，仅同步基线
            self._prev_core_health = cur
            return
        now = time.time()
        downs, recovers = [], []
        for n, ok in cur.items():
            prev = self._prev_core_health.get(n)
            label = app_config.SERVICES.get(n, {}).get("label", n)
            if prev is True and not ok:
                # 抖动抑制：同一服务 60s 内不重复弹（状态仍记录，恢复仍会提示）
                if now - self._alert_ts.get(n, 0) >= 60:
                    downs.append(label)
                    self._alert_ts[n] = now
                self._alerted_down.add(n)
            elif ok and n in self._alerted_down:
                recovers.append(label)
                self._alerted_down.discard(n)
        if downs:
            self.tray.showMessage(f"{self.brand['name']} · 服务掉线",
                                  "，".join(downs) + " 已离线，请检查。",
                                  QSystemTrayIcon.Critical, 6000)
            self._log("⚠ 掉线告警：" + "，".join(downs))
        if recovers:
            self.tray.showMessage(f"{self.brand['name']} · 已恢复",
                                  "，".join(recovers) + " 已恢复在线。",
                                  QSystemTrayIcon.Information, 4000)
            self._log("✓ 已恢复：" + "，".join(recovers))
        self._prev_core_health = cur

    def _pulse(self):
        """让处于「加载中」的能力黄点做透明度脉冲，传达「正在动」。无加载点时不重绘。"""
        if not self._pulse_dots:
            return
        import math
        self._pulse_phase += 0.20
        a = 0.30 + 0.50 * (0.5 + 0.5 * math.sin(self._pulse_phase))  # 0.30..0.80
        c = C_PARTIAL
        style = f"color: rgba({c.red()},{c.green()},{c.blue()},{a:.2f});"
        for d in self._pulse_dots:
            try:
                d.setStyleSheet(style)
            except RuntimeError:
                pass  # 控件已销毁

    def _fade_in(self, w, start: float = 0.3, dur: int = 180):
        """轻量淡入：状态切换时给控件一次柔和出现感（仅 MOTION 开启时）。
        动画结束后移除临时透明度特效，避免与卡片投影特效冲突或残留。"""
        if not ui.MOTION:
            return
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        a = QPropertyAnimation(eff, b"opacity", self)
        a.setDuration(dur)
        a.setStartValue(start)
        a.setEndValue(1.0)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.finished.connect(lambda: w.setGraphicsEffect(None))
        a.start()
        self._badge_fade = a   # 保活

    def _set_badge(self, text: str, variant: str):
        changed = getattr(self, "_badge_variant", None) != variant
        self._badge_variant = variant
        self.badge.setText(text)
        self.badge.setObjectName(variant)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        if changed:
            self._fade_in(self.badge)
        # 托盘状态灯 + 提示随之更新
        if getattr(self, "tray", None) is not None:
            col = {"BadgeOK": C_OK, "BadgeWarn": C_PARTIAL}.get(variant, C_DOWN)
            self.tray.setIcon(_dot_icon(col.name()))
            self.tray.setToolTip(f"{self.brand['name']} · {text.lstrip('●○ ').strip()}")
            if getattr(self, "_tray_status_action", None) is not None:
                self._tray_status_action.setText(text.strip())

    # ── 系统托盘（常驻运维入口；关窗=最小化到托盘，服务不停）──
    def _setup_tray(self):
        self.tray = None
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
        except Exception:
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_dot_icon(C_DOWN.name()))
        self.tray.setToolTip(f"{self.brand['name']} · 检测中…")
        menu = QMenu()
        self._tray_status_action = menu.addAction("●  检测中…")
        self._tray_status_action.setEnabled(False)   # 只读状态行
        menu.addSeparator()
        menu.addAction("显示主窗口", self._show_normal)
        menu.addAction("打开控制台", self.on_open_ui)
        menu.addSeparator()
        menu.addAction("开始直播换脸", self.on_boot)
        menu.addAction("停止全部", self.on_stop_all)
        menu.addSeparator()
        menu.addAction("🌐 官网", lambda: webbrowser.open(
            self.brand.get("website") or BRAND_DEFAULTS["website"]))
        menu.addAction("💬 联系客服", lambda: webbrowser.open(_support_url(self.brand)))
        menu.addAction("🧰 生成诊断包", self.on_diag_pack)
        if _update_enabled():
            menu.addAction("⬆ 检查更新", lambda: self._check_update_async(manual=True))
        menu.addSeparator()
        menu.addAction("退出", self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        # 有托盘时，关闭窗口=最小化到托盘，不应让 Qt 因「最后窗口关闭」而退出进程
        try:
            QApplication.instance().setQuitOnLastWindowClosed(False)
        except Exception:
            pass

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_normal()

    def _show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        self._really_quit = True
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, e):
        """有托盘时关窗 = 最小化到托盘（服务继续在后台运行）；退出请用托盘菜单。"""
        if self.tray is not None and not self._really_quit:
            e.ignore()
            self.hide()
            if not self._tray_hinted:
                self._tray_hinted = True
                self.tray.showMessage(
                    self.brand["name"],
                    "已最小化到托盘，服务仍在后台运行。右键托盘图标可「退出」。",
                    QSystemTrayIcon.Information, 4000)
            return
        super().closeEvent(e)

    def _apply_health(self, h: dict):
        if not h:
            for v in self.res_labels.values():
                v.setText("—")
            for key in self.res_bars:
                self._set_bar(key, 0, C_DOWN.name())
            return
        gpu = h.get("gpu_util")
        ram = h.get("ram_percent")
        pres = h.get("pressure", "—")
        if isinstance(gpu, (int, float)) and gpu >= 0:
            self.res_labels["gpu"].setText(f"{gpu:.0f}%")
            self._set_bar("gpu", gpu,
                          (C_ERROR.name() if gpu >= 95 else C_PARTIAL.name() if gpu >= 80 else C_OK.name()))
        else:
            self.res_labels["gpu"].setText("—")
            self._set_bar("gpu", 0, C_DOWN.name())
        # 显存：优先精确 GB（/health.gpu_mem_* 或 /api/backpressure.vram），否则回退压力等级文字
        used = h.get("gpu_mem_used")
        total = h.get("gpu_mem_total")
        vram = h.get("vram") or {}
        if not (isinstance(total, (int, float)) and total > 0):
            used, total = vram.get("used_mb"), vram.get("total_mb")
        pres_map = {"green": "充足", "yellow": "偏紧", "red": "紧张",
                    "low": "充足", "med": "偏紧", "high": "紧张"}
        if isinstance(total, (int, float)) and total > 0 and isinstance(used, (int, float)) and used >= 0:
            # 显存等级须按【显存占用率】判定，不能套用 /health.pressure（那是 GPU+内存口径）
            ratio = used / total
            lvl = "紧张" if ratio >= 0.92 else ("偏紧" if ratio >= 0.80 else "充足")
            self.res_labels["vram"].setText(f"{used/1024:.1f} / {total/1024:.0f} GB（{lvl}）")
            self._set_bar("vram", ratio * 100,
                          (C_ERROR.name() if ratio >= 0.92 else C_PARTIAL.name() if ratio >= 0.80 else C_OK.name()))
        else:
            self.res_labels["vram"].setText(pres_map.get(pres, str(pres)))
            self._set_bar("vram", 0, C_DOWN.name())
        if isinstance(ram, (int, float)) and ram >= 0:
            self.res_labels["ram"].setText(f"{ram:.0f}%")
            self._set_bar("ram", ram,
                          (C_ERROR.name() if ram >= 90 else C_PARTIAL.name() if ram >= 75 else C_OK.name()))
        else:
            self.res_labels["ram"].setText("—")
            self._set_bar("ram", 0, C_DOWN.name())

    # ── 操作 ────────────────────────────────────────────────
    def _run_bg(self, fn, note):
        if self.busy:
            return
        self._set_busy(True, note)

        def work():
            try:
                fn()
            except Exception as e:
                self.bridge.log.emit(f"出错: {e}")
            finally:
                self.bridge.op_done.emit()

        threading.Thread(target=work, daemon=True).start()

    # 每类页面的应用窗口初始尺寸（w, h）：控制台宽、对话/手机比例窄、看板/同传适中。
    _WIN_SIZES = (("/ui", (1440, 900)), ("/phone", (1100, 800)), ("/dashboard", (1280, 820)),
                  ("/ops", (1280, 820)), ("/help", (1100, 860)), ("/delivery", (1180, 820)))

    @classmethod
    def _page_win_size(cls, path: str):
        for prefix, size in cls._WIN_SIZES:
            if path.startswith(prefix):
                return size
        return (1280, 850)

    def _open_app_window(self, url: str, size: tuple = None):
        """以无边框「应用窗口」模式打开（Edge/Chrome --app）——无地址栏/标签页、独立任务栏图标，
        像独立软件窗口而非网页。找不到 Edge/Chrome 时回退系统默认浏览器，保证一定能打开。
        可用环境变量 AVATARHUB_APP_BROWSER 指定浏览器可执行文件。"""
        import shutil
        candidates = [
            os.environ.get("AVATARHUB_APP_BROWSER"),
            shutil.which("msedge"), shutil.which("chrome"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        exe = next((c for c in candidates if c and os.path.exists(c)), None)
        if exe:
            try:
                args = [exe, f"--app={url}", "--new-window"]
                if size:
                    args.append(f"--window-size={int(size[0])},{int(size[1])}")
                subprocess.Popen(args)
                return
            except Exception:
                pass
        webbrowser.open(url)

    def _open_hub_page(self, path: str):
        """工作台/能力卡通用入口：确保 Hub 就绪后，在独立应用窗口按预设尺寸打开 Hub 的某个页面
        （/ui#clone 克隆 · /phone 对话 · /dashboard 看板 …）。Hub 未起则先拉核心链路，就绪后自动打开。"""
        url = app_config.svc_url("hub") + path
        size = self._page_win_size(path)
        if sm.health_check(app_config.health_url("hub")):
            self._open_app_window(url, size)
            self._log(f"已打开 {url}")
            return

        def work():
            sm.start_all(required_only=True)
            hub_health = app_config.health_url("hub")
            for _ in range(90):
                if sm.health_check(hub_health):
                    self._open_app_window(url, size)
                    self.bridge.log.emit(f"已就绪，打开 {url}")
                    return
                time.sleep(1)
            self.bridge.log.emit("⚠ 核心服务 90 秒内仍未就绪。可点「查看详细日志」看进度，或用「一键体检」排查后重试。")

        self._run_bg(work, f"正在准备并打开（核心链路就绪后自动打开）…")

    def _maybe_warn_expired(self) -> bool:
        """试用/授权到期（且强制模式）时的软着陆提示：每次会话最多一次。
        绝不拦资产访问——服务照常可启，仅生成类被服务端暂停；返回 True=用户先去激活。"""
        if getattr(self, "_expired_warned", False):
            return False
        try:
            import license as _lic
            if not _lic.generation_blocked():
                return False
            msg = _lic.blocked_message()
        except Exception:
            return False
        self._expired_warned = True
        box = QMessageBox(self)
        box.setWindowTitle("试用已到期")
        box.setText(msg + "\n\n你仍可以启动服务、查看并导出全部角色与数据；"
                          "数字人开口/克隆等生成功能将在激活后立即恢复。")
        b_act = box.addButton("🔑 去激活 / 升级", QMessageBox.AcceptRole)
        box.addButton("先启动看看", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_act:
            self.on_license()
            return True
        return False

    def on_boot(self):
        """主 CTA：启动直播换脸链路 → 轮询直到 Hub 就绪 → 自动打开开播页。"""
        self._boot_attempted = True
        self._boot_ts = None
        if self._maybe_warn_expired():
            return

        def work():
            sm.start_all(required_only=True)
            hub_health = app_config.health_url("hub")
            opened = False
            for _ in range(90):  # 最多等 ~90s（首次加载模型更久时仍可手动「打开控制台」）
                if sm.health_check(hub_health):
                    url = app_config.svc_url("hub") + "/ui#stream"
                    self._open_app_window(url, (1440, 900))
                    self.bridge.log.emit(f"直播换脸开播页已就绪，已打开 {url}")
                    opened = True
                    break
                time.sleep(1)
            if not opened:
                self.bridge.log.emit("⚠ 核心服务 90 秒内仍未就绪。可点「查看详细日志」看进度，"
                                     "或用「一键体检」排查；首次加载大模型耗时较长时稍候再点本按钮即可。")

        self._run_bg(work, "正在启动直播换脸链路（就绪后自动打开开播页）…")

    def _demo_url(self) -> str:
        """Pick the best demo profile and return /phone URL. No exception escapes UI threads."""
        base = app_config.svc_url("hub")
        try:
            from urllib.request import urlopen
            from urllib.parse import quote
            import json as _json
            with urlopen(base + "/profiles", timeout=3) as r:
                d = _json.loads(r.read().decode("utf-8"))
            profiles = d.get("profiles") or []
            active = d.get("active") or ""
            chosen = next((p for p in profiles if p.get("name") == active), None)
            if not chosen:
                chosen = next((p for p in profiles if p.get("has_voice") and p.get("has_face")), None)
            if not chosen:
                chosen = next((p for p in profiles if p.get("has_voice")), None)
            if not chosen and profiles:
                chosen = profiles[0]
            if chosen and chosen.get("name"):
                return base + "/phone?profile=" + quote(chosen["name"])
        except Exception:
            pass
        return base + "/phone"

    def on_demo(self):
        """一键演示：确保核心服务在线，打开当前/最佳角色的客户体验页。"""
        self._boot_attempted = True

        def work():
            hub_health = app_config.health_url("hub")
            if not sm.health_check(hub_health):
                sm.start_all(required_only=True)
                for _ in range(90):
                    if sm.health_check(hub_health):
                        break
                    time.sleep(1)
            url = self._demo_url()
            self._open_app_window(url)
            self.bridge.log.emit(f"已打开一键演示体验页：{url}")

        self._run_bg(work, "正在准备一键演示（选择角色并打开体验页）…")

    def on_start_core(self):
        self._boot_attempted = True
        self._run_bg(lambda: sm.start_all(required_only=True),
                     "正在启动核心链路（首次加载模型可能需 1–2 分钟）…")

    def on_start_all(self):
        self._boot_attempted = True
        self._run_bg(lambda: sm.start_all(required_only=False),
                     "正在启动全部服务（含扩展，显存占用较高）…")

    def on_stop_all(self):
        if QMessageBox.question(self, "确认", "停止全部服务？正在进行的直播会中断。") != QMessageBox.Yes:
            return
        # 主动停服不是故障：清旗，避免随后把"停止"误判为失败而弹错误卡 / 进度条
        self._boot_attempted = False
        self._boot_ts = None
        self._hub_seen_ok = False    # 解除 Hub 看护武装：人停的服务绝不能被看护复活
        self._run_bg(sm.stop_all, "正在停止全部服务…")

    def on_restart_sel(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选择一个服务。")
            return
        name = self.table.item(rows[0].row(), 1).text()
        svc = next((s for s in sm.SERVICES if s["name"] == name), None)
        if not svc:
            return
        self._run_bg(lambda: (sm.stop_service(name), sm.start_service(svc)),
                     f"正在重启 {name} …")

    def _open_capability(self, idx: int):
        """点击能力卡 → 弹出该能力下的引擎明细（在线/延迟/许可/重启）。"""
        _, name, _ = PILLARS[idx]
        members = self.cap_refs[idx][2]
        CapabilityDialog(name, members, self).exec()

    def on_open_ui(self):
        """打开网页控制台：Hub 已就绪直接开；未就绪则先启动核心链路、就绪后自动打开，
        避免打开一个打不开的死页（点了就有反馈）。"""
        url = app_config.svc_url("hub") + "/ui"
        if sm.health_check(app_config.health_url("hub")):
            try:
                if pack_installer is not None:
                    pack_installer.confirm_app_ok(log=self._log)
            except Exception:
                pass
            self._open_app_window(url, (1440, 900))
            self._log(f"已打开 {url}")
            return

        def work():
            sm.start_all(required_only=True)
            hub_health = app_config.health_url("hub")
            for _ in range(90):
                if sm.health_check(hub_health):
                    # 程序热修运行验收：hub 健康即确认本代更新（清 probation，杜绝下次误回滚）
                    try:
                        if pack_installer is not None:
                            pack_installer.confirm_app_ok(log=lambda m: self.bridge.log.emit(m))
                    except Exception:
                        pass
                    self._open_app_window(url, (1440, 900))
                    self.bridge.log.emit(f"控制台已就绪，已打开 {url}")
                    return
                time.sleep(1)
            # 90s 仍不健康：若本会话刚应用过热修 → 大概率是坏热修"起得来编译过但跑不起来"，
            # 当场即时回滚+重启（缩短暴露窗口，不必等下次启动）。非热修导致的不健康不误伤。
            reverted = False
            try:
                if pack_installer is not None and pack_installer.app_probation_pending():
                    self.bridge.log.emit("⚠ 核心服务 90s 未就绪且本会话刚更新过程序 → 即时回滚上一代并重启。")
                    if pack_installer.app_revert(log=lambda m: self.bridge.log.emit(m)):
                        reverted = True
                        sm.stop_all()
                        time.sleep(2)
                        sm.start_all(required_only=True)
                        for _ in range(60):
                            if sm.health_check(hub_health):
                                self._open_app_window(url)
                                self.bridge.log.emit(f"已回滚并恢复，控制台已打开 {url}")
                                return
                            time.sleep(1)
            except Exception:
                pass
            self.bridge.log.emit("⚠ 核心服务 90 秒内仍未就绪。"
                                 + ("回滚后仍未恢复，" if reverted else "")
                                 + "可点「查看详细日志」看进度，或用「一键体检」排查后再次点击「打开控制台」。")

        self._run_bg(work, "控制台未就绪，正在启动核心服务，就绪后自动打开 …")

    def on_open_faceswap(self):
        """打开换脸面板（faceswap 独立服务 :8000/ui，扩展能力）：已在线直接开；未起则先拉该服务、就绪后打开。
        换脸是扩展能力（非核心链路），首次加载较慢或缺权重时给出明确提示而非死等。"""
        url = app_config.svc_url("faceswap") + "/ui"
        if sm.health_check(app_config.health_url("faceswap")):
            self._open_app_window(url, (1280, 850))
            self._log(f"已打开换脸面板 {url}")
            return
        svc = next((s for s in sm.SERVICES if s["name"] == "faceswap"), None)
        if not svc:
            QMessageBox.information(self, "换脸",
                                    "未找到换脸服务定义。换脸是扩展能力，需 facefusion 环境与权重。\n"
                                    "可在「高级运维 → 启动全部（含扩展）」拉起后重试。")
            return

        def work():
            sm.start_service(svc)
            fh = app_config.health_url("faceswap")
            for _ in range(60):
                if sm.health_check(fh):
                    self._open_app_window(url, (1280, 850))
                    self.bridge.log.emit(f"换脸面板已就绪，已打开 {url}")
                    return
                time.sleep(1)
            self.bridge.log.emit("⚠ 换脸服务 60 秒内未就绪（首次加载较慢或缺模型权重）。可用「一键体检」排查后重试。")

        self._run_bg(work, "正在启动换脸服务（扩展能力），就绪后自动打开 …")

    def on_open_interp(self):
        """打开实时同传(通译 LingoX)：已在线直接开；否则后台启动就绪后自动打开。"""
        url = app_config.svc_url("interpreter") + "/"
        if sm.health_check(app_config.health_url("interpreter")):
            self._open_app_window(url, (1280, 820))
            self._log(f"已打开同传 {url}")
            return
        svc = next((s for s in sm.SERVICES if s["name"] == "interpreter"), None)
        if not svc:
            self._log("未找到 interpreter 服务定义")
            return
        self._run_bg(lambda: (sm.start_service(svc), self._open_app_window(url, (1280, 820))),
                     "正在启动同传服务，就绪后自动打开 …")

    def on_open_interp_live(self):
        """一键直播同传：拉起依赖栈 → 打开同传页并自动开直播模式。"""
        url = app_config.svc_url("interpreter") + "/?live=1&go=1"

        def work():
            res = sm.start_live_stack()
            bad = [k for k, v in res.items() if not v]
            if bad:
                self._log(f"⚠ 部分服务未就绪: {', '.join(bad)}")
            else:
                self._log("直播同传链路已就绪")
            self._open_app_window(url, (1280, 820))
            self._log(f"已打开直播同传 {url}")

        self._run_bg(work, "正在启动直播同传链路(中枢/识别/TTS/口型/广播/同传)…")

    def _run_in_console(self, script_name: str, note: str):
        py = app_config.conda_python("facefusion")
        script = str(app_config.BASE / script_name)
        try:
            subprocess.Popen(["cmd", "/k", py, script],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                             cwd=str(app_config.BASE))
            self._log(note)
        except Exception as e:
            self._log(f"启动失败: {e}")

    def on_components(self):
        """手动打开组件向导：加装更高档位 / 修复缺失组件。"""
        if pack_installer is None:
            QMessageBox.information(self, "提示", "组件安装内核不可用（pack_installer 缺失）。")
            return
        src = resolve_manifest_source()
        if not src:
            QMessageBox.information(self, "提示",
                                    "未找到 manifest（需 manifest.json 在程序目录，或设 AVATARHUB_MANIFEST_URL）。")
            return
        try:
            manifest, src_root = pack_installer.load_manifest(src)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"manifest 解析失败：{e}")
            return
        FirstRunWizard(manifest, src_root, self).exec()

    def on_maintenance(self):
        """维护：切换发布通道（stable/beta）+ 一键回滚到历史版本（出问题的安全网）。"""
        if pack_installer is None:
            QMessageBox.information(self, "提示", "组件安装内核不可用（pack_installer 缺失）。")
            return
        src = resolve_update_source() or resolve_manifest_source()
        if not src:
            QMessageBox.information(self, "提示",
                                    "未找到 manifest（需 manifest.json 在程序目录，或设 AVATARHUB_MANIFEST_URL）。")
            return
        try:
            manifest, src_root = pack_installer.load_manifest(src)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"manifest 解析失败：{e}")
            return
        MaintenanceDialog(manifest, src_root, self).exec()

    def on_license(self):
        LicenseDialog(self).exec()

    def on_settings(self):
        SettingsDialog(self).exec()

    def notify_updates_async(self):
        """启动后后台检查组件更新；有更新则在日志区提示（不打扰）。
        关键：更新检查优先拉【远端】manifest（base_url/manifest_url），否则只对本地清单
        自比对将永远发现不了下载站上的新 pack。"""
        if pack_installer is None:
            return
        src = resolve_update_source()
        if not src:
            return

        def work():
            try:
                manifest, src_root = pack_installer.load_manifest(src)
                try:
                    pack_installer._ACTIVE_SRC_ROOT = pack_installer.resolve_sources(manifest, src_root)
                except Exception:
                    pack_installer._ACTIVE_SRC_ROOT = [src_root]   # 灰度准入取控制通道(密钥B)
                # 记住生效的上报端点（远端 manifest 可能比本地新）→ 崩溃上报不依赖本地 manifest 新鲜度
                try:
                    import telemetry_client as _tc
                    if manifest.get("telemetry_url"):
                        _tc.remember_endpoint(manifest["telemetry_url"], manifest.get("telemetry_token", ""))
                except Exception:
                    pass
                # exe 自更新：manifest 带 exe 块且版本/ sha 与当前不同 → 下载暂存，退出时摆渡替换
                try:
                    exe_blk = manifest.get("exe") or {}
                    import sys as _sys
                    if getattr(_sys, "frozen", False) and exe_blk.get("file") and exe_blk.get("sha256"):
                        cur_exe = Path(_sys.executable)
                        cur_sha = pack_installer._sha256(cur_exe) if cur_exe.exists() else ""
                        if cur_sha != exe_blk["sha256"]:
                            srcs = pack_installer.resolve_sources(manifest, src_root)
                            dst = pack_installer.CACHE_DIR / "AvatarHub.new.exe"
                            pack_installer.download(pack_installer._resolve_src(srcs[0], exe_blk["file"]),
                                                    dst, exe_blk["sha256"], log=self.bridge.log.emit,
                                                    mirrors=[pack_installer._resolve_src(r, exe_blk["file"]) for r in srcs[1:]])
                            if pack_installer.stage_exe_update(dst, log=self.bridge.log.emit):
                                self.bridge.log.emit("✅ 控制台新版已就绪，下次退出重启时自动生效。")
                except Exception:
                    pass
                s = pack_installer.update_summary(manifest)
                if not s["count"]:
                    return
                detail = "、".join(f"{it['id']}（{it['human']}）" for it in s["items"][:6])
                more = "…" if s["count"] > 6 else ""
                self.bridge.log.emit(
                    f"发现 {s['count']} 个组件可更新，本次约需下载 {s['human']}："
                    f"{detail}{more}。")
                # 轻量更新（仅程序本体 app + 小组件，总量 ≤ 阈值）→ 后台静默预下载并就绪，
                # 用户无需点「组件」。大组件（环境/模型）仍走手动确认，避免偷跑大流量。
                ups = pack_installer.check_updates(manifest)
                small = [(cid, c) for cid, c in ups
                         if cid.startswith("app:") or c.get("size_bytes", 0) <= 64 * 1024 * 1024]
                small_bytes = sum(c.get("size_bytes", 0) for _, c in small)
                auto = _load_settings().get("auto_update_small", True)
                if auto and small and small_bytes <= 128 * 1024 * 1024:
                    self.bridge.log.emit(f"正在后台静默下载轻量更新（{pack_installer._human(small_bytes)}，"
                                         f"含程序热修）…直播/会话中不会打断。")
                    srcs = pack_installer.resolve_sources(manifest, src_root)
                    failed = pack_installer.install_components(manifest, small, srcs, log=self.bridge.log.emit)
                    applied = [cid for cid, _ in small if cid.startswith("app:")]
                    if not failed:
                        if applied and pack_installer._load_json(pack_installer.APP_PENDING_FILE):
                            self.bridge.log.emit("✅ 程序更新已就绪，将在下次启动（或退出直播后）自动应用，可随时「设置→回滚」。")
                        else:
                            self.bridge.log.emit("✅ 轻量更新已完成。")
                else:
                    self.bridge.log.emit("点「组件」按钮即可增量更新（未变组件不会重下）。")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def on_doctor(self):
        self._run_in_console("doctor.py", "已在新窗口运行一键体检 doctor.py。")

    def on_provision(self):
        self._run_in_console("provision.py", "已在新窗口运行环境体检 provision.py。")

    def on_acceptance(self):
        """一键验收：按《交付与验收清单》逐项核验本机部署（用实时运行信号，无需 conda/dist）。"""
        AcceptanceDialog(self).exec()

    def on_open_checklist(self):
        """打开随包的《交付与验收清单.md》（系统默认程序）。"""
        doc = app_config.BASE / "交付与验收清单.md"
        if not doc.exists():
            QMessageBox.information(
                self, "提示",
                "未找到《交付与验收清单.md》。\n它随安装包/便携版一起分发，请向分发方索取，"
                "或在「一键验收」里直接逐项核验。")
            return
        try:
            os.startfile(str(doc))  # noqa: Windows only
            self._log("已打开《交付与验收清单》。")
        except Exception as e:
            self._log(f"打开验收清单失败：{e}")


# ══════════════════════════════════════════════════════════════════
#  首启向导（下载/安装分发包）
# ══════════════════════════════════════════════════════════════════
def fmt_eta(sec: float) -> str:
    """剩余时间友好格式：s / m s / h m；非正/无穷显示「—」。"""
    if sec <= 0 or sec != sec or sec == float("inf"):
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _settings_path():
    return app_config.BASE / "launcher_settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(d: dict):
    try:
        _settings_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── 开机自启（HKCU Run 键；仅对打包 exe 有意义）────────────────────────
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "AvatarHub"


def _autostart_target() -> str | None:
    """开机自启要写入的命令；仅冻结成 exe 后才稳定（脚本路径在用户机上不可靠）。
    带 --minimized：开机自启时直接进托盘，不弹主窗口。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return None


def is_autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _RUN_NAME)
            return bool(val)
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enable: bool) -> bool:
    """开/关开机自启；成功返回 True。非 Windows / 非打包 / 无权限 → False。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                tgt = _autostart_target()
                if not tgt:
                    return False
                winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, tgt)
            else:
                try:
                    winreg.DeleteValue(k, _RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def current_channel() -> str:
    """当前发布通道：环境变量 AVATARHUB_CHANNEL > 本地设置 launcher_settings.json > 空（stable 默认）。"""
    return (os.environ.get("AVATARHUB_CHANNEL", "").strip()
            or _load_settings().get("channel", "").strip())


def _apply_channel(src: str | None) -> str | None:
    """若已选通道且内核可用，把入口（channels.json / 带 channels_url 的 manifest）解析到该通道的 manifest。"""
    if not src or pack_installer is None:
        return src
    ch = current_channel()
    if not ch:
        return src
    try:
        return pack_installer.resolve_channel_source(src, ch)
    except Exception:
        return src


def resolve_manifest_source() -> str | None:
    """manifest 来源：环境变量 AVATARHUB_MANIFEST_URL > 安装目录\\manifest.json。
    开发机通常无 manifest（在 dist/ 下），故向导不会误弹。"""
    url = os.environ.get("AVATARHUB_MANIFEST_URL", "").strip()
    if url:
        return _apply_channel(url)
    local = app_config.BASE / "manifest.json"
    return _apply_channel(str(local)) if local.exists() else None


def resolve_update_source() -> str | None:
    """更新检查的 manifest 来源：环境变量 > 本地 manifest 内的 manifest_url/base_url（远端）> 本地文件。
    把新 pack 发布到下载站后，无需重发应用即可被用户端「更新检查」发现。"""
    url = os.environ.get("AVATARHUB_MANIFEST_URL", "").strip()
    if url:
        return _apply_channel(url)
    local = app_config.BASE / "manifest.json"
    if not local.exists():
        return None
    try:
        m = json.loads(local.read_text(encoding="utf-8"))
        remote = (m.get("manifest_url") or "").strip()
        if not remote:
            base = (m.get("base_url") or "").strip()
            if base.startswith(("http://", "https://")):
                remote = base.rstrip("/") + "/manifest.json"
        if remote.startswith(("http://", "https://")):
            return _apply_channel(remote)
    except Exception:
        pass
    return _apply_channel(str(local))


def _hub_alive() -> bool:
    """Hub 已在 :9000 健康响应 → 系统肯定已就位（无论 conda 还是便携包模式）。"""
    try:
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:9000/health", timeout=1.5) as r:
            return getattr(r, "status", r.getcode()) == 200
    except Exception:
        return False


def _core_envs_present() -> bool:
    """核心链路所需 conda 环境是否都在（conda 部署模式：已装好但未启动也算就位）。

    注意 conda_python() 找不到环境时会回退「当前解释器」——冻结态那是 AvatarHub.exe 自身，
    并非环境就位。1.0.3 在无 conda 的新装机上被判「已就位」→ 首启向导被跳过、什么都没下载，
    start_all 还会拿 exe 当 python 反复重生启动器副本（单实例守卫令其秒退 code=0）。
    故：解释器必须是真实存在的 python.exe，且不能是当前进程自己。"""
    try:
        from pathlib import Path as _P
        import app_config
        core = {s["env"] for s in app_config.SERVICES.values() if s.get("core")}
        if not core:
            return False
        self_exe = _P(sys.executable)
        for e in core:
            p = _P(app_config.conda_python(e))
            if p.name.lower() != "python.exe" or not p.exists() or p == self_exe:
                return False
        return True
    except Exception:
        return False


def _runtime_envs_installed() -> bool:
    """首启向导装出的自包含环境（runtime\\envs\\<env>\\python.exe）是否至少一个就位。
    与 _core_envs_present 的区别：只认「本产品自己装的」环境。自动启动服务只对这种机器
    是安全的——自部署/生产机上的 conda 环境不归启动器管，贸然 start_all 会清理端口，
    可能误杀正在跑的生产服务（1.0.3 在 198 标机上差点杀掉口型服务，实锤）。"""
    try:
        root = app_config.BASE / "runtime" / "envs"
        return any(p.is_file() for p in root.glob("*/python.exe"))
    except Exception:
        return False


def needs_first_run(manifest: dict) -> bool:
    """没有任何环境组件就位 → 视为首次运行，需安装。
    例外：Hub 已在线 或 核心 conda 环境已就位 → 系统已装好，直接进控制台（避免对已部署机误弹安装向导）。"""
    if pack_installer is None:
        return False
    if _hub_alive() or _core_envs_present():
        return False
    return not any(pack_installer.is_installed(cid, c)
                   for cid, c in pack_installer.iter_components(manifest)
                   if cid.startswith("env:"))


class _WizBridge(QObject):
    detected = Signal(object, object, object)   # gpus, best, runnable
    progress = Signal(float, float, str)        # done_bytes, total_bytes, cid
    log = Signal(str)
    done = Signal(bool, str, object)             # ok, message, failed[(cid,comp)]
    source = Signal(str, int)                    # 生效源 url, 源总数


class FirstRunWizard(QDialog):
    """验机 → 选档 → 下载安装（进度条）。逻辑全在 pack_installer，本类只做 UI。"""

    def __init__(self, manifest: dict, src_root: str, parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.src_root = src_root
        self.installing = False
        self.last_failed = []          # [(cid, comp)] 供「重试失败项」
        self._spd_hist = []            # [(t, done_bytes)] 速度滑窗
        self.bridge = _WizBridge()
        self.bridge.detected.connect(self._on_detected)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.log.connect(lambda m: self.logbox.appendPlainText(m))
        self.bridge.done.connect(self._on_done)
        self.bridge.source.connect(self._on_source)
        self._build_ui()
        QTimer.singleShot(50, self._detect_async)

    _STEPS = ("验机", "选档", "安装")

    def _build_ui(self):
        self.brand = _dlg_brand(self.parent())
        self.step = 0
        self._detected = False
        self.setWindowTitle(f"{self.brand['name']} · 首次安装向导")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(620, 500)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(12)

        v.addWidget(_brand_header(
            self.brand, f"首次安装向导　·　v{self.manifest.get('version','?')}"))

        # 步骤指示器
        steprow = QHBoxLayout()
        steprow.setSpacing(8)
        self.step_lbls = []
        for i, name in enumerate(self._STEPS):
            lbl = QLabel(f"{i+1}　{name}")
            lbl.setFont(uifont(10, QFont.Bold))
            steprow.addWidget(lbl)
            self.step_lbls.append(lbl)
            if i < len(self._STEPS) - 1:
                arr = QLabel("›")
                arr.setObjectName("Sub")
                steprow.addWidget(arr)
        steprow.addStretch(1)
        v.addLayout(steprow)

        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)

        # —— Step 1：验机 ——
        p0 = QWidget()
        l0 = QVBoxLayout(p0)
        l0.setContentsMargins(0, 6, 0, 0)
        l0.setSpacing(8)
        cap0 = QLabel("第一步：检测本机显卡与装机环境，自动推荐可流畅运行的版本档位。")
        cap0.setObjectName("CapName")
        cap0.setWordWrap(True)
        self.gpu_label = QLabel("正在检测显卡…")
        self.gpu_label.setObjectName("Sub")
        self.gpu_label.setWordWrap(True)
        self.disk_lbl = QLabel("检测磁盘空间…")
        self.disk_lbl.setObjectName("Sub")
        self.disk_lbl.setWordWrap(True)
        self.write_lbl = QLabel("")
        self.write_lbl.setObjectName("Sub")
        self.write_lbl.setWordWrap(True)
        l0.addWidget(cap0)
        l0.addWidget(self.gpu_label)
        l0.addWidget(self.disk_lbl)
        l0.addWidget(self.write_lbl)
        l0.addStretch(1)
        self.stack.addWidget(p0)

        # —— Step 2：选档 ——
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 6, 0, 0)
        l1.setSpacing(8)
        cap1 = QLabel("已按你的显卡自动推荐版本，点「开始安装」即可一键完成。"
                      "如需更低/更高档位可在下拉中更换（灰色为当前显存不足）。")
        cap1.setObjectName("CapName")
        cap1.setWordWrap(True)
        l1.addWidget(cap1)
        row = QHBoxLayout()
        row.addWidget(QLabel("版本："))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(360)
        self.combo.currentIndexChanged.connect(self._refresh_size)
        row.addWidget(self.combo)
        row.addStretch(1)
        l1.addLayout(row)
        self.size_label = QLabel("")
        self.size_label.setObjectName("Sub")
        self.size_label.setWordWrap(True)
        l1.addWidget(self.size_label)
        self.feat_label = QLabel("")
        self.feat_label.setWordWrap(True)
        self.feat_label.setTextFormat(Qt.RichText)
        l1.addWidget(self.feat_label)
        l1.addStretch(1)
        self.stack.addWidget(p1)

        # —— Step 3：安装 ——
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 6, 0, 0)
        l2.setSpacing(8)
        cap2 = QLabel("正在下载运行环境与 AI 模型：首次需一次性下载，之后每次启动无需重复下载；"
                      "完成后将自动进入控制台。请保持联网，勿关闭本窗口。")
        cap2.setObjectName("CapName")
        cap2.setWordWrap(True)
        l2.addWidget(cap2)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        l2.addWidget(self.progress)
        self.comp_label = QLabel("")      # 正在下载：第 n/m 个组件 · 名称
        self.comp_label.setObjectName("Sub")
        self.comp_label.setWordWrap(True)
        l2.addWidget(self.comp_label)
        self.speed_label = QLabel("")     # 速度 · 预计剩余时间 · 已下载/总量
        self.speed_label.setObjectName("Sub")
        l2.addWidget(self.speed_label)
        self.src_label = QLabel("")
        self.src_label.setObjectName("Sub")
        l2.addWidget(self.src_label)
        self.feat_label2 = QLabel("")     # 下载等待期给用户看「装完能干什么」
        self.feat_label2.setWordWrap(True)
        self.feat_label2.setTextFormat(Qt.RichText)
        l2.addWidget(self.feat_label2)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        l2.addWidget(self.logbox, 1)
        self.stack.addWidget(p2)

        # 导航
        bar = QHBoxLayout()
        self.btn_cancel = QPushButton("稍后")
        self.btn_cancel.setObjectName("ghost")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_back = QPushButton("上一步")
        self.btn_back.setObjectName("ghost")
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.clicked.connect(lambda: self._goto(self.step - 1))
        bar.addWidget(self.btn_cancel)
        bar.addWidget(self.btn_back)
        bar.addStretch(1)
        self.btn_primary = QPushButton("下一步")
        self.btn_primary.setObjectName("primary")
        self.btn_primary.setCursor(Qt.PointingHandCursor)
        self.btn_primary.clicked.connect(self._on_primary)
        bar.addWidget(self.btn_primary)
        v.addLayout(bar)

        # 兼容旧引用：失败时复用 btn_primary 作为「重试失败项」按钮
        self.btn_install = self.btn_primary
        self._free_gb = None
        self._run_install_preflight()
        self._goto(0)

    def _set_pf(self, lbl, level: str, text: str):
        col = {"ok": C_OK.name(), "warn": C_PARTIAL.name(), "crit": C_ERROR.name()}[level]
        ic = {"ok": "✓", "warn": "⚠", "crit": "✗"}[level]
        lbl.setText(f"{ic}  {text}")
        lbl.setStyleSheet(f"color: {col};")

    def _run_install_preflight(self):
        """装前本地预检（不连 Hub、不查 conda——首装时环境本就还没装，避免误报）：
        只看真正影响「装得下/装得进」的项：磁盘可用空间 + 安装目录可写。"""
        import shutil
        try:
            free = shutil.disk_usage(str(app_config.BASE)).free / 1e9
            self._free_gb = free
            if free < 10:
                self._set_pf(self.disk_lbl, "crit",
                             f"安装盘可用空间仅 {free:.0f}GB（运行环境+模型通常需 ≥20GB）")
            elif free < 20:
                self._set_pf(self.disk_lbl, "warn",
                             f"安装盘可用空间 {free:.0f}GB 偏低（建议 ≥20GB）")
            else:
                self._set_pf(self.disk_lbl, "ok", f"安装盘可用空间 {free:.0f}GB")
        except Exception:
            self.disk_lbl.setText("磁盘空间检测失败")
        try:
            t = app_config.BASE / ".pf_write_test"
            t.write_text("x", encoding="utf-8")
            t.unlink()
            self._set_pf(self.write_lbl, "ok", "安装目录可写")
        except Exception:
            self._set_pf(self.write_lbl, "crit", "安装目录不可写（请安装到可写目录或授予权限）")

    def _goto(self, step: int):
        step = max(0, min(step, len(self._STEPS) - 1))
        self.step = step
        self.stack.setCurrentIndex(step)
        for i, lbl in enumerate(self.step_lbls):
            if i < step:
                lbl.setStyleSheet(f"color: {C_OK.name()};")       # 已完成
            elif i == step:
                lbl.setStyleSheet(f"color: {self.brand['color']};")  # 当前
            else:
                lbl.setStyleSheet("color: #6a7079;")               # 待办
        self._sync_nav()

    def _sync_nav(self):
        # 「稍后」在验机/选档两步都可见（一键化会自动停在选档页），方便用户先跳过安装。
        self.btn_cancel.setVisible(self.step in (0, 1) and not self.installing)
        # 自动跳到选档页时无需「上一步」（验机页无操作）；仅在安装页保留返回改档。
        self.btn_back.setVisible(self.step == 2 and not self.installing)
        if self.step == 0:
            self.btn_primary.setText("下一步")
            self.btn_primary.setEnabled(self._detected)
        elif self.step == 1:
            self.btn_primary.setText("开始安装")
            self.btn_primary.setEnabled(bool(self.combo.currentData()))
        else:
            if self.installing:
                self.btn_primary.setText("安装中…")
                self.btn_primary.setEnabled(False)
            elif self.last_failed:
                self.btn_primary.setText(f"重试失败项 ({len(self.last_failed)})")
                self.btn_primary.setEnabled(True)
            else:
                self.btn_primary.setText("完成")
                self.btn_primary.setEnabled(True)

    def _on_primary(self):
        if self.step == 0:
            self._goto(1)
        elif self.step == 1:
            self._goto(2)
            self._on_install()
        else:
            if self.last_failed:
                self._on_install()
            else:
                self.accept()

    # 按显存给用户一份「装完你能干什么」清单（选档页 + 下载等待页都展示）。
    # 阈值与 pack_installer.EDITION_MIN_VRAM_GB 对齐：lite=6 / standard=16 / flagship=24。
    _FEATURES = (
        ("声音克隆 · 变声直播", 6),
        ("图片 / 视频换脸 · 美颜美妆", 8),
        ("实时数字人直播（口型同步）", 16),
        ("高清数字人 + AI 同声传译", 24),
    )

    def _update_features(self):
        vram = getattr(self, "_vram_gb", 0.0)
        rows = []
        for name, need in self._FEATURES:
            if vram >= need:
                rows.append(f"<span style='color:{C_OK.name()};'>✓ {name}</span>")
            else:
                rows.append(f"<span style='color:#6a7079;'>✗ {name}（需 ≥{need}GB 显存）</span>")
        html = "<b>根据你的显卡，本机可用功能：</b><br>" + "<br>".join(rows)
        self.feat_label.setText(html)
        self.feat_label2.setText(html)

    def _detect_async(self):
        def work():
            gpus = pack_installer.detect_gpus()
            best, runnable = pack_installer.recommend_edition(self.manifest, gpus)
            self.bridge.detected.emit(gpus, best, runnable)
        threading.Thread(target=work, daemon=True).start()

    def _on_detected(self, gpus, best, runnable):
        self._vram_gb = max((g["total_mb"] for g in gpus), default=0) / 1024.0
        self._update_features()
        if gpus:
            g = gpus[0]
            self.gpu_label.setText(
                f"显卡：{g['name']}　显存 {g['total_mb']/1024:.0f}GB（空闲 {g['free_mb']/1024:.1f}GB）")
        else:
            self.gpu_label.setText("未检测到 NVIDIA 显卡——实时数字人需要 GPU（仍可安装，但可能无法运行）。")
        self.combo.clear()
        for ed, spec in self.manifest.get("editions", {}).items():
            ok = runnable.get(ed, False)
            label = spec.get("label", ed) + ("" if ok else "　[显存不足]")
            self.combo.addItem(label, ed)
            if not ok:
                self.combo.model().item(self.combo.count() - 1).setEnabled(False)
        if best:
            for i in range(self.combo.count()):
                if self.combo.itemData(i) == best:
                    self.combo.setCurrentIndex(i)
                    break
        self._refresh_size()
        self._detected = True
        self._sync_nav()
        # 一键化：检测到可流畅运行的推荐档位后，直接跳到单步「一键安装」，用户点一下即可开始；
        # 无 GPU / 显存不足（best 为空）时留在验机页给出提示，仍可手动选择较低档或稍后再装。
        if best and runnable.get(best):
            self._goto(1)

    def _refresh_size(self):
        ed = self.combo.currentData()
        if not ed:
            return
        try:
            p = pack_installer.plan(self.manifest, ed)
            dl = p["download_bytes"]
            self._todo_total = len(p["todo"])
            base = (f"需下载 {len(p['todo'])} 个组件，合计约 {pack_installer._human(dl)}"
                    f"（已就位 {len(p['have'])} 个）")
            # 解压后体积通常显著大于压缩包；用 ~2.5× 粗估「落地占用」，与可用空间比对预警
            need_gb = dl * 2.5 / 1e9
            if self._free_gb is not None and dl > 0 and self._free_gb < need_gb:
                self.size_label.setText(
                    base + f"　⚠ 含解压预计约需 {need_gb:.0f}GB，超过当前可用 {self._free_gb:.0f}GB，请先清理空间")
                self.size_label.setStyleSheet(f"color: {C_ERROR.name()};")
            else:
                self.size_label.setText(base)
                self.size_label.setStyleSheet("")
        except Exception:
            self.size_label.setText("")

    def _on_install(self):
        if self.installing:
            return
        ed = self.combo.currentData()
        if not ed:
            return
        retry = self.last_failed                  # 有失败项则本次仅重试它们
        self.installing = True
        self._spd_hist = []
        self._seen_cids = []                      # 组件计数（第 n/m 个）
        if retry:
            self._todo_total = len(retry)
        self._sync_nav()

        def work():
            try:
                def overall(done, total, cid):
                    self.bridge.progress.emit(float(done), float(total), cid)
                # 多源择优 + failover：探测放在工作线程（避免阻塞 UI）。
                roots = pack_installer.resolve_sources(self.manifest, self.src_root)
                if len(roots) > 1:
                    self.bridge.log.emit(f"发现 {len(roots)} 个下载源，按延迟择优（失败自动切换镜像）…")
                    roots = pack_installer.order_sources(
                        roots, log=lambda m: self.bridge.log.emit(m))
                self.bridge.source.emit(roots[0] if roots else "", len(roots))
                if retry:
                    self.bridge.log.emit(f"仅重试上次失败的 {len(retry)} 个组件…")
                    failed = pack_installer.install_components(
                        self.manifest, retry, roots,
                        on_overall=overall, log=lambda m: self.bridge.log.emit(m))
                else:
                    failed = pack_installer.install_edition(
                        self.manifest, ed, roots,
                        on_overall=overall, log=lambda m: self.bridge.log.emit(m))
                # 把失败 cid 映射回 comp，供「重试失败项」
                cmap = {cid: c for cid, c in pack_installer.iter_components(self.manifest)}
                fail_comps = [(cid, cmap[cid]) for cid, _ in failed if cid in cmap]
                if failed:
                    names = "、".join(cid for cid, _ in failed)
                    self.bridge.done.emit(
                        False, f"{len(failed)} 个组件未完成（{names}），其余已就位。", fail_comps)
                else:
                    self.bridge.done.emit(True, "安装完成，即将进入控制台。", [])
            except Exception as e:
                self.bridge.done.emit(False, f"安装失败：{e}", [])
        threading.Thread(target=work, daemon=True).start()

    _fmt_eta = staticmethod(fmt_eta)

    @staticmethod
    def _cid_human(cid: str) -> str:
        kind, _, name = cid.partition(":")
        return {"env": "运行环境", "model": "AI 模型", "shared": "基础库"}.get(kind, kind) + " · " + name

    def _on_progress(self, done: float, total: float, cid: str):
        now = time.time()
        self._spd_hist.append((now, done))
        while len(self._spd_hist) > 1 and now - self._spd_hist[0][0] > 3.0:
            self._spd_hist.pop(0)
        speed = 0.0
        if len(self._spd_hist) >= 2:
            dt = self._spd_hist[-1][0] - self._spd_hist[0][0]
            db = self._spd_hist[-1][1] - self._spd_hist[0][1]
            speed = (db / dt) if dt > 0 else 0.0
        pct = (done / total * 100) if total else 100
        self.progress.setValue(int(pct))
        self.progress.setFormat(f"总进度 {pct:.1f}%")
        if cid and cid not in self._seen_cids:
            self._seen_cids.append(cid)
        if cid:
            m = max(getattr(self, "_todo_total", 0), len(self._seen_cids))
            self.comp_label.setText(
                f"正在下载：{self._cid_human(cid)}（第 {len(self._seen_cids)}/{m} 个组件）")
        h = pack_installer._human
        eta = ((total - done) / speed) if speed > 0 else 0
        self.speed_label.setText(
            f"速度 {h(speed)}/s · 预计还需 {self._fmt_eta(eta)} · 已下载 {h(done)}/{h(total)}")

    def _on_source(self, src: str, n: int):
        if not src:
            return
        tip = f"生效下载源：{src}" + (f"（共 {n} 源，失败自动切换）" if n > 1 else "")
        self.src_label.setText(tip)

    def _on_done(self, ok: bool, msg: str, failed=None):
        self.installing = False
        self.last_failed = failed or []
        self.logbox.appendPlainText(msg)
        self._sync_nav()
        if ok:
            # 热加载 config.json，让本次会话立即用上新登记的 conda_python 映射。
            try:
                app_config.CONFIG = app_config._load_config()
            except Exception:
                pass
            QTimer.singleShot(800, self.accept)
        else:
            # 失败即给出口：重试之外，把「找人」做成按钮（首启失败=最容易流失的时刻）
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("安装失败")
            box.setText(msg + "\n\n可点「重试失败项」继续（已完成部分不会重下）；"
                              "多次失败请联系客服，发送本页截图可最快定位。")
            box.addButton("知道了", QMessageBox.AcceptRole)
            b_sup = box.addButton("💬 联系客服", QMessageBox.ActionRole)
            box.exec()
            if box.clickedButton() is b_sup:
                try:
                    webbrowser.open(_support_url(resolve_brand()))
                except Exception:
                    pass


class _MaintBridge(QObject):
    populated = Signal(object, object)      # channels(list[str]), rollback_points(list[dict])
    progress = Signal(float, float, str)    # done, total, cid
    log = Signal(str)
    done = Signal(bool, str)                # ok, msg
    sla_done = Signal(bool)                 # STT 实时闭环自检完成（ok）


class MaintenanceDialog(QDialog):
    """维护：切换发布通道（stable/beta）+ 一键回滚到历史版本。复用 B-11 的速度/ETA 呈现、
    B-12 的 list_rollback_points/rollback_to 内核。通道切换写入 launcher_settings.json 当场生效。"""

    def __init__(self, manifest: dict, src_root: str, parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.src_root = src_root
        self.busy = False
        self._spd_hist = []
        self.rollback_pts = []
        self.bridge = _MaintBridge()
        self.bridge.populated.connect(self._on_populated)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.log.connect(lambda m: self.logbox.appendPlainText(m))
        self.bridge.done.connect(self._on_done)
        self.bridge.sla_done.connect(self._on_sla_done)
        self._build_ui()
        QTimer.singleShot(30, self._load_async)

    def _build_ui(self):
        self.brand = _dlg_brand(self.parent())
        self.setWindowTitle(f"{self.brand['name']} · 维护（通道 / 回滚）")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(640, 480)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(10)

        v.addWidget(_brand_header(self.brand, "维护中心　·　发布通道 / 一键回滚"))
        ver = self.manifest.get("version", "?")
        self.head = QLabel(f"当前版本 v{ver}　通道：{current_channel() or 'stable（默认）'}")
        self.head.setObjectName("Sub")
        v.addWidget(self.head)

        # 通道切换
        crow = QHBoxLayout()
        crow.addWidget(QLabel("发布通道："))
        self.chan_combo = QComboBox()
        self.chan_combo.setMinimumWidth(200)
        crow.addWidget(self.chan_combo)
        self.btn_chan = self._mk("应用通道", "accent", self._on_apply_channel)
        crow.addWidget(self.btn_chan)
        crow.addStretch(1)
        v.addLayout(crow)
        hint = QLabel("切到 beta 可尝鲜新版；切回 stable 求稳。应用后点「组件」检查/更新即按新通道。")
        hint.setObjectName("Sub")
        v.addWidget(hint)

        # 回滚
        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("回滚到："))
        self.rb_combo = QComboBox()
        self.rb_combo.setMinimumWidth(280)
        rrow.addWidget(self.rb_combo)
        self.btn_rb = self._mk("回滚到所选版本", "danger", self._on_rollback)
        rrow.addWidget(self.btn_rb)
        rrow.addStretch(1)
        v.addLayout(rrow)
        rhint = QLabel("仅回退与目标版不同的组件（旧包从其版本目录就近取回并校验）；升级出问题时的安全网。")
        rhint.setObjectName("Sub")
        v.addWidget(rhint)

        # 匿名健康回执（opt-in）
        self.chk_tele = QCheckBox("允许发送匿名安装健康回执（仅成败/耗时/组件名，无任何个人信息，可随时关闭）")
        if telemetry is not None:
            self.chk_tele.setChecked(telemetry.enabled())
            self.chk_tele.toggled.connect(lambda on: telemetry.set_enabled(on))
        else:
            self.chk_tele.setEnabled(False)
        v.addWidget(self.chk_tele)

        # 崩溃报告（默认开，可关）：仅栈签名+脱敏摘要，用于修 bug；人脸/声音/视频永不上传。
        self.chk_crash = QCheckBox("发生错误时发送匿名崩溃报告（仅出错位置，帮助我们更快修复；内容数据绝不上传）")
        try:
            import telemetry_client as _tcm
            self._tcm = _tcm
            self.chk_crash.setChecked(_tcm.crash_enabled())
            self.chk_crash.toggled.connect(lambda on: _tcm.set_crash_enabled(on))
        except Exception:
            self._tcm = None
            self.chk_crash.setEnabled(False)
        v.addWidget(self.chk_crash)

        # 轻量更新静默下载（默认开）：程序热修与小组件后台就绪，直播/会话中不打断。
        self.chk_autoupd = QCheckBox("自动下载轻量更新（程序热修/小组件，后台就绪，直播中不打断；大模型仍手动）")
        self.chk_autoupd.setChecked(bool(_load_settings().get("auto_update_small", True)))

        def _save_autoupd(on):
            s = _load_settings()
            s["auto_update_small"] = bool(on)
            _save_settings(s)
        self.chk_autoupd.toggled.connect(_save_autoupd)
        v.addWidget(self.chk_autoupd)

        # 程序回滚（app 组件）：把上次热修一键退回上一代快照，升级出锅的安全网。
        arow = QHBoxLayout()
        self.btn_app_revert = self._mk("回滚上次程序更新", "danger", self._on_app_revert)
        arow.addWidget(self.btn_app_revert)
        self.btn_feedback = self._mk("一键反馈问题", "ghost", self._on_feedback)
        arow.addWidget(self.btn_feedback)
        self.btn_view_tele = self._mk("查看我发送过什么", "ghost", self._on_view_telemetry)
        arow.addWidget(self.btn_view_tele)
        arow.addStretch(1)
        v.addLayout(arow)

        v.addWidget(self._build_sla_group())

        self.progress = QProgressBar()
        self.progress.setValue(0)
        v.addWidget(self.progress)
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("Sub")
        v.addWidget(self.speed_label)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        v.addWidget(self.logbox, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_close = self._mk("关闭", "ghost", self.reject)
        bar.addWidget(self.btn_close)
        v.addLayout(bar)

    def _mk(self, text, kind, slot):
        b = QPushButton(text)
        b.setObjectName(kind)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _build_sla_group(self) -> QGroupBox:
        """STT 实时闭环达标线（写 config.json[stt_sla]，供 interp_selfcheck/发布门禁读取）。"""
        cur = app_config.stt_sla()
        box = QGroupBox("STT 实时 SLA 达标线（验收 / 发布门禁用）")
        g = QHBoxLayout(box)
        g.setSpacing(8)

        self.sla_first = QSpinBox(); self.sla_first.setRange(50, 10000)
        self.sla_first.setSingleStep(50); self.sla_first.setSuffix(" ms")
        self.sla_first.setValue(int(cur.get("first_p95", 700)))
        self.sla_final = QSpinBox(); self.sla_final.setRange(100, 20000)
        self.sla_final.setSingleStep(50); self.sla_final.setSuffix(" ms")
        self.sla_final.setValue(int(cur.get("final_p95", 1500)))
        self.sla_rate = QDoubleSpinBox(); self.sla_rate.setRange(0.0, 1.0)
        self.sla_rate.setSingleStep(0.01); self.sla_rate.setDecimals(2)
        self.sla_rate.setValue(float(cur.get("ok_rate", 0.95)))
        self.sla_conc = QSpinBox(); self.sla_conc.setRange(0, 256)
        self.sla_conc.setValue(int(cur.get("target_c", 0)))

        g.addWidget(QLabel("首partial p95")); g.addWidget(self.sla_first)
        g.addWidget(QLabel("final p95")); g.addWidget(self.sla_final)
        g.addWidget(QLabel("成功率≥")); g.addWidget(self.sla_rate)
        g.addWidget(QLabel("目标并发(0=阶梯最大)")); g.addWidget(self.sla_conc)
        g.addWidget(self._mk("保存", "accent", self._on_save_sla))
        self.btn_sla_run = self._mk("立即自检", "ghost", self._on_run_sla)
        g.addWidget(self.btn_sla_run)
        g.addStretch(1)
        return box

    def _on_save_sla(self):
        try:
            app_config.update_config({"stt_sla": {
                "first_p95": int(self.sla_first.value()),
                "final_p95": int(self.sla_final.value()),
                "ok_rate": round(float(self.sla_rate.value()), 4),
                "target_c": int(self.sla_conc.value()),
            }})
            self.logbox.appendPlainText(
                f"[SLA] 已保存到 config.json：首{self.sla_first.value()}ms / "
                f"final{self.sla_final.value()}ms / 成功率≥{self.sla_rate.value():.2f} / "
                f"并发{self.sla_conc.value() or '阶梯最大'}（环境变量 AVATARHUB_SLA_* 仍优先）")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"写入 config.json 失败：{e}")

    def _on_run_sla(self):
        """立即跑实时闭环自检（barge-in + 并发阶梯 + SLA），流式回显到日志区。
        先保存当前阈值，确保自检用最新达标线；需 Hub/nemo_stt/GPU 就绪。"""
        if getattr(self, "_sla_busy", False):
            return
        self._on_save_sla()
        self._sla_busy = True
        self.btn_sla_run.setEnabled(False)
        self.btn_sla_run.setText("自检中…")
        self.logbox.appendPlainText(
            "[SLA] 开始实时闭环自检（并发阶梯 1,4,8）… 需 Hub/nemo_stt/GPU 就绪，请稍候")

        def work():
            rc = -1
            try:
                py = app_config.conda_python("facefusion")
                if not os.path.exists(py):
                    py = sys.executable
                cmd = [py, "interp_selfcheck.py", "--stt-bench", "1,4,8", "--ci"]
                proc = subprocess.Popen(
                    cmd, cwd=str(app_config.BASE), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    self.bridge.log.emit(line.rstrip())
                rc = proc.wait()
                self.bridge.log.emit(
                    "[SLA] 结论：达标 ✓（exit 0）" if rc == 0
                    else f"[SLA] 结论：未达标 / 服务未就绪 ✗（exit {rc}）")
            except Exception as e:
                self.bridge.log.emit(f"[SLA] 自检异常：{e}")
            finally:
                self.bridge.sla_done.emit(rc == 0)

        threading.Thread(target=work, daemon=True).start()

    def _on_sla_done(self, ok: bool):
        self._sla_busy = False
        self.btn_sla_run.setEnabled(True)
        self.btn_sla_run.setText("立即自检")

    def _load_async(self):
        def work():
            chans = []
            try:
                curl = self.manifest.get("channels_url")
                if curl:
                    data = pack_installer._read_json(curl)
                    chans = list((data.get("channels") or {}).keys())
            except Exception:
                pass
            try:
                pts = pack_installer.list_rollback_points(self.manifest)
            except Exception:
                pts = []
            self.bridge.populated.emit(chans, pts)
        threading.Thread(target=work, daemon=True).start()

    def _on_populated(self, chans, pts):
        self.rollback_pts = pts or []
        cur = current_channel() or "stable"
        items = list(dict.fromkeys((chans or []) + ["stable", "beta"] + ([cur] if cur else [])))
        self.chan_combo.clear()
        self.chan_combo.addItems(items)
        if cur in items:
            self.chan_combo.setCurrentText(cur)
        self.rb_combo.clear()
        if self.rollback_pts:
            for p in self.rollback_pts:
                self.rb_combo.addItem(f"v{p['version']}　（{p['date'][:10]}）", p["manifest_url"])
        else:
            self.rb_combo.addItem("（无可回滚的历史版本）", "")
            self.btn_rb.setEnabled(False)

    def _on_apply_channel(self):
        ch = self.chan_combo.currentText().strip()
        s = _load_settings()
        s["channel"] = ch
        _save_settings(s)
        os.environ["AVATARHUB_CHANNEL"] = ch     # 当场生效
        self.head.setText(f"当前版本 v{self.manifest.get('version','?')}　通道：{ch}")
        self.logbox.appendPlainText(f"已切换到通道「{ch}」。点主界面「组件」即按新通道检查/安装。")

    def _set_busy(self, b: bool):
        self.busy = b
        self.btn_rb.setEnabled(not b and bool(self.rollback_pts))
        self.btn_chan.setEnabled(not b)
        self.btn_close.setEnabled(not b)

    def _on_view_telemetry(self):
        """打开本机遥测目录（runtime/telemetry）——把"可审计"从文档承诺变成一键可达。"""
        try:
            d = app_config.BASE / "runtime" / "telemetry"
            d.mkdir(parents=True, exist_ok=True)
            n = len(list(d.glob("*.json"))) + (1 if (d / "queue.jsonl").exists() else 0)
            try:
                os.startfile(str(d))   # Windows 资源管理器打开
            except Exception:
                webbrowser.open(d.as_uri())
            self._log(f"已打开本机遥测目录（{d}），共 {n} 份可审计记录。这里就是本机发送/待发的全部内容。")
        except Exception as e:
            QMessageBox.information(self, "查看遥测", f"打开失败：{e}\n目录：{app_config.BASE / 'runtime' / 'telemetry'}")

    def _on_feedback(self):
        """一键反馈：打包脱敏日志+环境，发送前给用户预览，确认后上报为 feedback 事件。"""
        try:
            import telemetry_client as _tc
        except Exception:
            QMessageBox.information(self, "反馈", "反馈组件不可用（缺 telemetry_client）。")
            return
        note, ok = QInputDialog.getMultiLineText(
            self, "反馈问题", "请描述遇到的问题（选填联系方式在下一步）：", "")
        if not ok:
            return
        contact, _ = QInputDialog.getText(self, "联系方式（选填）", "微信/TG/邮箱，便于我们回访：")
        ev = _tc.build_feedback(note=note or "", contact=contact or "")
        import json as _json
        preview = _json.dumps(ev, ensure_ascii=False, indent=2)
        if len(preview) > 4000:
            preview = preview[:4000] + "\n…（日志已截断）"
        box = QMessageBox(self)
        box.setWindowTitle("发送前预览（内容已脱敏）")
        box.setText("以下内容将匿名发送给我们用于排查（人脸/声音/视频等内容数据不在其中）。确认发送？")
        box.setDetailedText(preview)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return

        def work():
            ok2 = _tc.send_feedback(ev)
            self.bridge.log.emit("反馈已发送，谢谢！我们会在后续版本跟进。" if ok2
                                 else "反馈发送失败（网络或未配置上报端点），可稍后重试或直接联系客服。")
        threading.Thread(target=work, daemon=True).start()

    def _on_app_revert(self):
        if getattr(self, "busy", False) or pack_installer is None:
            return
        if QMessageBox.question(
                self, "回滚程序更新",
                "把「程序本体」回退到上一代快照？用于热修出问题时兜底。\n"
                "只影响程序代码（不动已下载的模型/环境），回滚后重启生效。") != QMessageBox.Yes:
            return

        def work():
            try:
                ok = pack_installer.app_revert(log=lambda m: self.bridge.log.emit(m))
                self.bridge.log.emit("已回滚程序，请重启控制台生效。" if ok else "没有可回滚的程序快照。")
            except Exception as e:
                self.bridge.log.emit(f"程序回滚失败：{e}")
        threading.Thread(target=work, daemon=True).start()

    def _on_rollback(self):
        if self.busy:
            return
        murl = self.rb_combo.currentData()
        if not murl:
            return
        ver = self.rb_combo.currentText()
        if QMessageBox.question(
                self, "确认回滚",
                f"确定回滚到 {ver}？将回退与该版本不同的组件，期间请勿关闭。") != QMessageBox.Yes:
            return
        self._set_busy(True)
        self._spd_hist = []

        def work():
            try:
                def overall(done, total, cid):
                    self.bridge.progress.emit(float(done), float(total), cid)
                failed = pack_installer.rollback_to(
                    murl, on_overall=overall, log=lambda m: self.bridge.log.emit(m))
                if failed:
                    names = "、".join(cid for cid, _ in failed)
                    self.bridge.done.emit(False, f"{len(failed)} 个组件回退失败（{names}），可重试。")
                else:
                    self.bridge.done.emit(True, "回滚完成。建议重启服务以加载旧版组件。")
            except Exception as e:
                self.bridge.done.emit(False, f"回滚失败：{e}")
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: float, total: float, cid: str):
        now = time.time()
        self._spd_hist.append((now, done))
        while len(self._spd_hist) > 1 and now - self._spd_hist[0][0] > 3.0:
            self._spd_hist.pop(0)
        speed = 0.0
        if len(self._spd_hist) >= 2:
            dt = self._spd_hist[-1][0] - self._spd_hist[0][0]
            db = self._spd_hist[-1][1] - self._spd_hist[0][1]
            speed = (db / dt) if dt > 0 else 0.0
        pct = (done / total * 100) if total else 100
        self.progress.setValue(int(pct))
        self.progress.setFormat(f"{pct:.1f}%  {cid}")
        h = pack_installer._human
        eta = ((total - done) / speed) if speed > 0 else 0
        self.speed_label.setText(f"速度 {h(speed)}/s · 剩余 {fmt_eta(eta)} · {h(done)}/{h(total)}")

    def _on_done(self, ok: bool, msg: str):
        self._set_busy(False)
        self.logbox.appendPlainText(msg)
        (QMessageBox.information if ok else QMessageBox.warning)(self, "回滚", msg)


# ══════════════════════════════════════════════════════════════════
#  一键验收（按《交付与验收清单》逐项核验本机部署）
# ══════════════════════════════════════════════════════════════════
def _detect_gpu() -> str | None:
    """返回 NVIDIA 显卡名（首块），无则 None。用 nvidia-smi，不弹控制台。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


class _AcceptBridge(QObject):
    row = Signal(dict)       # 单项结果
    finished = Signal(dict)  # 汇总 {rows, crit, warn}
    log = Signal(str)
    recheck = Signal()       # 开机完成 → 回主线程重新检测


class AcceptanceDialog(QDialog):
    """一键验收：用【实时运行信号】逐项核验，与《交付与验收清单》§3 对应。
    全部检查只读本机现状（显卡/磁盘/可写/环境/Hub/服务/引擎/控制台），
    不依赖 conda base、不依赖 dist 构建产物——在真实合作伙伴机上即可跑。"""

    _LVL = {"ok": (C_OK.name(), "✓"), "warn": (C_PARTIAL.name(), "⚠"),
            "crit": (C_ERROR.name(), "✗"), "info": (C_DOWN.name(), "◦")}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._busy = False
        self.bridge = _AcceptBridge()
        self.bridge.row.connect(self._on_row)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.log.connect(lambda m: self.logbox.appendPlainText(m))
        self.bridge.recheck.connect(self._start)
        self._build_ui()
        QTimer.singleShot(60, self._start)

    def _build_ui(self):
        self.brand = _dlg_brand(self.parent())
        self.setWindowTitle(f"{self.brand['name']} · 一键验收")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(640, 560)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(10)

        v.addWidget(_brand_header(self.brand, "一键验收　·　按《交付与验收清单》逐项核验本机部署"))

        self.summary = QLabel("正在检测…")
        self.summary.setObjectName("Badge")
        self.summary.setFont(uifont(11, QFont.Bold))
        v.addWidget(self.summary)

        tip = QLabel("建议先点主界面「一键开机」，待服务就绪后再验收，结果更完整。")
        tip.setObjectName("Sub")
        v.addWidget(tip)

        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(4)
        holder = QWidget()
        holder.setLayout(self.rows_box)
        v.addWidget(holder)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFixedHeight(90)
        v.addWidget(self.logbox, 1)

        bar = QHBoxLayout()
        self.btn_boot = self._mk("一键开机并复检", "accent", self._boot_and_recheck)
        self.btn_rerun = self._mk("重新检测", "ghost", self._start)
        self.btn_copy = self._mk("复制报告", "ghost", self._copy_report)
        self.btn_save = self._mk("保存报告", "ghost", self._save_report)
        bar.addWidget(self.btn_boot)
        bar.addWidget(self.btn_rerun)
        bar.addWidget(self.btn_copy)
        bar.addWidget(self.btn_save)
        bar.addStretch(1)
        bar.addWidget(self._mk("关闭", "ghost", self.reject))
        v.addLayout(bar)
        self._btns = [self.btn_boot, self.btn_rerun, self.btn_copy, self.btn_save]

    def _mk(self, text, kind, slot):
        b = QPushButton(text)
        b.setObjectName(kind)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _clear_rows(self):
        while self.rows_box.count():
            it = self.rows_box.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _start(self):
        if self._busy:
            return
        self._busy = True
        self._rows = []
        self._clear_rows()
        self.summary.setText("正在检测…")
        for b in self._btns:
            b.setEnabled(False)
        threading.Thread(target=self._run_checks, daemon=True).start()

    def _boot_and_recheck(self):
        """在验收页直接一键开机（启动核心链路），就绪后自动重新检测——省去回主界面再来一趟。"""
        if self._busy:
            return
        self._busy = True
        for b in self._btns:
            b.setEnabled(False)
        self.summary.setText("正在一键开机（启动核心链路，首次加载模型可能需 1–2 分钟）…")
        self.summary.setObjectName("BadgeWarn")
        self.summary.setStyleSheet(build_style(self.brand["color"]))
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)
        threading.Thread(target=self._boot_work, daemon=True).start()

    def _boot_work(self):
        try:
            self.bridge.log.emit("正在启动核心链路…")
            sm.start_all(required_only=True)
            hub_health = app_config.health_url("hub")
            ready = False
            for i in range(90):  # 最多等 ~90s
                if sm.health_check(hub_health):
                    ready = True
                    break
                if i and i % 10 == 0:
                    self.bridge.log.emit(f"仍在加载模型…（已等待 {i}s）")
                time.sleep(1)
            self.bridge.log.emit("✓ Hub 已就绪，开始复检。" if ready
                                 else "Hub 仍在加载，先复检当前状态（稍后可再点「重新检测」）。")
        except Exception as e:
            self.bridge.log.emit(f"开机失败：{e}")
        finally:
            self._busy = False
            self.bridge.recheck.emit()

    # ── 逐项检查（工作线程；只读实时信号）──────────────────────────
    def _run_checks(self):
        import shutil
        rows = []

        def emit(key, label, level, detail):
            r = {"key": key, "label": label, "level": level, "detail": detail}
            rows.append(r)
            self.bridge.row.emit(r)

        # 1) 显卡
        name = _detect_gpu()
        if name:
            emit("gpu", "显卡", "ok", name)
        else:
            emit("gpu", "显卡", "crit", "未检测到 NVIDIA 显卡（nvidia-smi 不可用）")

        # 2) 磁盘可用空间
        try:
            free = shutil.disk_usage(str(app_config.BASE)).free / 1e9
            lvl = "ok" if free >= 20 else ("warn" if free >= 10 else "crit")
            emit("disk", "磁盘可用空间", lvl, f"{free:.0f}GB（建议 ≥20GB）")
        except Exception as e:
            emit("disk", "磁盘可用空间", "warn", f"检测失败：{type(e).__name__}")

        # 3) 安装目录可写
        try:
            t = app_config.BASE / ".accept_write_test"
            t.write_text("x", encoding="utf-8")
            t.unlink()
            emit("write", "安装目录可写", "ok", str(app_config.BASE))
        except Exception:
            emit("write", "安装目录可写", "crit", "不可写（请换可写目录或授予权限）")

        # 4) 核心运行环境
        hub = _hub_alive()
        if _core_envs_present():
            emit("env", "核心运行环境", "ok", "核心 conda 环境已就位")
        elif hub:
            emit("env", "核心运行环境", "ok", "Hub 在线（便携/已部署模式）")
        else:
            emit("env", "核心运行环境", "warn", "核心环境未就位（可用首启向导 /「组件」安装）")

        # 5) 中枢 Hub
        emit("hub", "中枢 Hub", "ok" if hub else "warn",
             "在线 :9000" if hub else "未启动（点「一键开机」后再验收）")

        # 6) 核心服务
        try:
            st = sm.get_status()
            core = [n for n, s in app_config.SERVICES.items() if s.get("core")]
            ready = [n for n in core if st.get(n, {}).get("healthy")]
            loading = [n for n in core
                       if st.get(n, {}).get("running") and not st.get(n, {}).get("healthy")]
            if core and len(ready) == len(core):
                emit("svc", "核心服务", "ok", f"{len(ready)}/{len(core)} 就绪")
            elif ready or loading:
                emit("svc", "核心服务", "warn",
                     f"{len(ready)}/{len(core)} 就绪，加载中 {len(loading)}")
            else:
                emit("svc", "核心服务", "warn", f"0/{len(core)} 就绪（未启动）")
        except Exception as e:
            emit("svc", "核心服务", "warn", f"探测失败：{type(e).__name__}")

        # 7) 能力引擎
        try:
            from urllib.request import urlopen
            import json as _json
            with urlopen("http://127.0.0.1:9000/api/engines", timeout=2.5) as r:
                data = _json.loads(r.read().decode("utf-8"))
            engs = data.get("engines", [])
            avail = [e for e in engs if e.get("available")]
            if engs:
                emit("eng", "能力引擎", "ok" if avail else "warn",
                     f"{len(avail)}/{len(engs)} 引擎可用")
            else:
                emit("eng", "能力引擎", "warn", "引擎列表为空")
        except Exception:
            emit("eng", "能力引擎", "warn", "Hub 未在线，无法查询引擎（先启动）")

        # 8) 控制台可达
        try:
            from urllib.request import urlopen
            with urlopen("http://127.0.0.1:9000/ui", timeout=2.5) as r:
                ok = getattr(r, "status", r.getcode()) == 200
            emit("ui", "控制台可达", "ok" if ok else "warn", "http://127.0.0.1:9000/ui")
        except Exception:
            emit("ui", "控制台可达", "warn", "未响应（先点「一键开机」）")

        # 9) STT 实时 SLA（读最近一次闭环实测报告，不现跑——现跑会扰动直播管线）
        try:
            reps = sorted((app_config.BASE / "logs").glob("stt_closedloop_report_*.json"))
            if not reps:
                emit("sla", "STT 实时 SLA", "info",
                     "尚无实测报告（「维护」设达标线后运行 interp_selfcheck --stt-bench 生成）")
            else:
                rp = reps[-1]
                age_h = (time.time() - rp.stat().st_mtime) / 3600
                age = f"{age_h:.0f}h前" if age_h >= 1 else "1h内"
                rep = json.loads(rp.read_text(encoding="utf-8-sig"))  # 容忍 BOM
                sla = rep.get("sla") or {}
                if not sla:
                    emit("sla", "STT 实时 SLA", "info",
                         f"最近闭环（{age}）未设 SLA 阈值；判定结论 {rep.get('verdict', '?')}")
                else:
                    emit("sla", "STT 实时 SLA", "ok" if sla.get("ok") else "warn",
                         f"{sla.get('summary', '?')}（{age}实测）")
        except Exception as e:
            emit("sla", "STT 实时 SLA", "info", f"报告解析失败：{type(e).__name__}")

        crit = sum(1 for r in rows if r["level"] == "crit")
        warn = sum(1 for r in rows if r["level"] == "warn")
        self.bridge.finished.emit({"rows": rows, "crit": crit, "warn": warn})

    # ── 渲染 ──────────────────────────────────────────────────────
    def _on_row(self, r: dict):
        self._rows.append(r)
        col, ic = self._LVL.get(r["level"], self._LVL["info"])
        w = QFrame()
        w.setObjectName("Card")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 8, 12, 8)
        icon = QLabel(ic)
        icon.setStyleSheet(f"color: {col};")
        icon.setFont(uifont(12, QFont.Bold))
        icon.setFixedWidth(22)
        lab = QLabel(r["label"])
        lab.setFont(uifont(10, QFont.Bold))
        lab.setFixedWidth(130)
        det = QLabel(r["detail"])
        det.setObjectName("Sub")
        det.setWordWrap(True)
        h.addWidget(icon)
        h.addWidget(lab)
        h.addWidget(det, 1)
        self.rows_box.addWidget(w)

    def _on_finished(self, res: dict):
        self._busy = False
        crit, warn = res["crit"], res["warn"]
        if crit:
            self.summary.setText(f"●  验收未通过：{crit} 项需处理"
                                 + (f"，{warn} 项待启动/可优化" if warn else ""))
            self.summary.setObjectName("BadgeDown")
        elif warn:
            self.summary.setText(f"●  基本就绪：{warn} 项待启动/可优化（建议先「一键开机」）")
            self.summary.setObjectName("BadgeWarn")
        else:
            self.summary.setText("●  全部通过 ✓  本机部署验收合格")
            self.summary.setObjectName("BadgeOK")
        self.summary.setStyleSheet(build_style(self.brand["color"]))
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)
        for b in self._btns:
            b.setEnabled(True)

    # ── 报告 ──────────────────────────────────────────────────────
    def _report_text(self) -> str:
        lines = [f"{self.brand['name']} 部署验收报告",
                 f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"目录：{app_config.BASE}", ""]
        for r in self._rows:
            _, ic = self._LVL.get(r["level"], self._LVL["info"])
            lines.append(f"{ic} {r['label']}：{r['detail']}")
        crit = sum(1 for r in self._rows if r["level"] == "crit")
        warn = sum(1 for r in self._rows if r["level"] == "warn")
        lines += ["", f"结论：{'未通过' if crit else ('基本就绪' if warn else '全部通过')}"
                      f"（需处理 {crit} · 待启动/可优化 {warn}）"]
        return "\n".join(lines)

    def _copy_report(self):
        QApplication.clipboard().setText(self._report_text())
        self.logbox.appendPlainText("验收报告已复制到剪贴板。")

    def _save_report(self):
        try:
            d = app_config.BASE / "logs"
            d.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            (d / f"acceptance_{ts}.txt").write_text(self._report_text(), encoding="utf-8")
            payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "base": str(app_config.BASE), "rows": self._rows}
            (d / f"acceptance_{ts}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.logbox.appendPlainText(f"验收报告已保存到 logs\\acceptance_{ts}.txt / .json")
        except Exception as e:
            self.logbox.appendPlainText(f"保存失败：{e}")


class LicenseDialog(QDialog):
    """授权与激活：查看状态 / 复制机器指纹 / 输兑换码在线激活 / 粘贴或导入 license.key 激活。"""

    online_done = Signal(bool, object)   # 在线激活在后台线程完成后回主线程

    def __init__(self, parent=None):
        super().__init__(parent)
        import license as lic
        self.lic = lic
        self._build_ui()
        self.online_done.connect(self._after_activate)
        self._refresh()

    def _build_ui(self):
        self.brand = _dlg_brand(self.parent())
        self.setWindowTitle(f"{self.brand['name']} · 授权与激活")
        self.setStyleSheet(build_style(self.brand["color"]))
        self.resize(600, 460)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(10)

        v.addWidget(_brand_header(self.brand, "授权与激活　·　机器指纹 / 兑换码 / license.key"))

        self.status_label = QLabel("…")
        self.status_label.setObjectName("Summary")
        self.status_label.setFont(uifont(12))
        v.addWidget(self.status_label)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("Sub")
        self.detail_label.setWordWrap(True)
        v.addWidget(self.detail_label)

        # 升级路径一句话讲清（试用为什么存在 + 怎么转正式），并给客服直达
        _supp = _support_url(self.brand)
        self.upgrade_label = QLabel(
            f'升级正式版：① 点「购买 / 续费」在官网下单（自动绑定本机指纹）；'
            f'② 或把机器指纹发给 <a href="{_supp}" style="color:{self.brand["color"]}">在线客服</a>'
            f'，收到兑换码/授权码后回到本页激活，全程不到 1 分钟。')
        self.upgrade_label.setObjectName("Sub")
        self.upgrade_label.setWordWrap(True)
        self.upgrade_label.setOpenExternalLinks(True)
        v.addWidget(self.upgrade_label)

        # 机器指纹（发给厂商签发授权用）
        fp_row = QHBoxLayout()
        fp_row.addWidget(QLabel("机器指纹："))
        self.fp_edit = QLineEdit()
        self.fp_edit.setReadOnly(True)
        fp_row.addWidget(self.fp_edit, 1)
        btn_copy = QPushButton("复制")
        btn_copy.setObjectName("ghost")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.clicked.connect(self._copy_fp)
        fp_row.addWidget(btn_copy)
        v.addLayout(fp_row)

        # 在线激活（输订单号，自助换授权）。未配置激活服务器时整行隐藏（白标可关）。
        # 1.0.9 起默认指向官网订单后端：填订单号即取回已签授权，免去粘贴一长串 base64。
        self._has_oa = False
        try:
            self._has_oa = bool(self.lic.activation_configured())
        except Exception:
            pass
        if self._has_oa:
            oa_row = QHBoxLayout()
            oa_row.addWidget(QLabel("订单号："))
            self.oa_edit = QLineEdit()
            self.oa_edit.setPlaceholderText("填订单号（形如 AH-20260713-ABCD），点「在线激活」自动取授权")
            oa_row.addWidget(self.oa_edit, 1)
            self.btn_oa = QPushButton("在线激活")
            self.btn_oa.setObjectName("primary")
            self.btn_oa.setCursor(Qt.PointingHandCursor)
            self.btn_oa.clicked.connect(self._activate_online)
            oa_row.addWidget(self.btn_oa)
            v.addLayout(oa_row)
        else:
            self.oa_edit = None
            self.btn_oa = None

        v.addWidget(QLabel("粘贴授权码（订单页「授权码」全文，或 license.key 内容）："))
        self.code_box = QPlainTextEdit()
        self.code_box.setPlaceholderText(
            "官网付款开通后，在订单状态页复制「授权码」粘贴到这里，点「激活」即可。")
        self.code_box.setFixedHeight(110)
        v.addWidget(self.code_box)

        bar = QHBoxLayout()
        btn_buy = QPushButton("🛒 购买 / 续费")
        btn_buy.setObjectName("primary")
        btn_buy.setCursor(Qt.PointingHandCursor)
        btn_buy.setToolTip("打开官网购买页（自动带上本机指纹，付款开通后回来粘贴授权码即可）")
        btn_buy.clicked.connect(self._open_store)
        bar.addWidget(btn_buy)
        btn_import = QPushButton("导入 license.key 文件")
        btn_import.setObjectName("ghost")
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.clicked.connect(self._import_file)
        bar.addWidget(btn_import)
        bar.addStretch(1)
        btn_act = QPushButton("激活")
        btn_act.setObjectName("primary")
        btn_act.setCursor(Qt.PointingHandCursor)
        btn_act.clicked.connect(self._activate_text)
        bar.addWidget(btn_act)
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("ghost")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)
        v.addLayout(bar)

    def _refresh(self):
        try:
            st = self.lic.load_state(force=True)
            pub = st.to_public()
        except Exception as e:
            self.status_label.setText("授权状态读取失败")
            self.detail_label.setText(str(e))
            return
        days = "永久" if st.days_left is None or st.days_left < 0 else f"剩余 {st.days_left} 天"
        if st.status == "trial":
            self.status_label.setText(f"试用版 · 全功能开放 · {days}")
        else:
            wm = "去水印" if st.features.get("watermark_free") else "带水印"
            self.status_label.setText(
                f"{pub.get('edition_label')} · {pub.get('status_label')} · {days} · {wm}")
        licensee = f"　被授权方：{st.licensee}" if st.licensee else ""
        self.detail_label.setText(f"{st.message}{licensee}")
        self.fp_edit.setText(st.this_machine or self.lic.machine_fingerprint())

    def _copy_fp(self):
        QApplication.clipboard().setText(self.fp_edit.text())
        QMessageBox.information(self, "已复制", "机器指纹已复制，发送给厂商以签发授权。")

    def _open_store(self):
        """打开官网购买页并带上本机指纹：下单即绑机，付款开通后状态页自取授权码。"""
        base = os.environ.get("AVATARHUB_STORE_URL", "https://usdt2026.cc/order").rstrip("/")
        fp = (self.fp_edit.text() or "").strip()
        url = f"{base}?fp={fp}" if fp else base
        try:
            webbrowser.open(url)
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开浏览器：{e}\n请手动访问 {url}")

    def _activate_text(self):
        ok, res = self.lic.activate_from_text(self.code_box.toPlainText())
        self._after_activate(ok, res)

    def _activate_online(self):
        code = self.oa_edit.text().strip()
        if not code:
            QMessageBox.information(self, "提示", "请先输入兑换码。")
            return
        self.btn_oa.setEnabled(False)
        self.btn_oa.setText("激活中…")

        def work():
            ok, res = self.lic.activate_online(code)
            self.online_done.emit(ok, res)
        threading.Thread(target=work, daemon=True).start()

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 license.key", "", "License (*.key *.json);;所有文件 (*.*)")
        if not path:
            return
        ok, res = self.lic.activate_from_file(path)
        self._after_activate(ok, res)

    def _after_activate(self, ok, res):
        if self.btn_oa is not None:
            self.btn_oa.setEnabled(True)
            self.btn_oa.setText("在线激活")
        if ok:
            self.code_box.clear()
            if self.oa_edit is not None:
                self.oa_edit.clear()
            self._refresh()
            QMessageBox.information(self, "激活成功", f"已激活：{res.message}")
        else:
            QMessageBox.warning(self, "激活失败", str(res))


def maybe_show_privacy_consent(parent=None) -> None:
    """首启隐私同意页（只弹一次，settings 记标记）：透明告知采集边界 + 两个开关。
    崩溃报告默认勾（帮助修 bug、仅栈签名），使用统计默认不勾（opt-in）。内容数据永不上传。"""
    s = _load_settings()
    if s.get("privacy_ack"):
        return
    try:
        import telemetry_client as _tc
    except Exception:
        _tc = None
    try:
        import telemetry as _tele
    except Exception:
        _tele = None
    dlg = QDialog(parent)
    dlg.setWindowTitle("隐私与改进（首次运行）")
    try:
        _sc = ui.UI_SCALE
    except Exception:
        _sc = 1.0
    dlg.setMinimumWidth(int(520 * _sc))
    v = QVBoxLayout(dlg)
    title = QLabel("帮助我们把产品做得更好")
    title.setStyleSheet("font-size:16px;font-weight:600;")
    v.addWidget(title)
    body = QLabel(
        "本产品本地运行、数据不出机。为了更快修复问题、按真实使用改进功能，"
        "可选择匿名回传少量诊断信息：\n\n"
        "· 崩溃报告：仅出错位置（栈签名）与脱敏日志摘要，用于修 bug；\n"
        "· 使用统计：版本/显卡档位/功能次数等匿名计数，用于优化方向。\n\n"
        "绝不上传：人脸/声音/视频等任何内容数据、角色资产、文件路径、账号信息。\n"
        "以上随时可在「组件/设置」里开关；企业可完全关闭或指向自建服务器。\n"
        "所有回传内容先落本机 runtime/telemetry（可审计），设置页「查看我发送过什么」一键可看。")
    body.setWordWrap(True)
    body.setObjectName("Sub")
    v.addWidget(body)
    chk_crash = QCheckBox("发送匿名崩溃报告（推荐，帮助更快修复）")
    chk_crash.setChecked(True)
    chk_usage = QCheckBox("发送匿名使用统计（可选）")
    chk_usage.setChecked(False)
    v.addWidget(chk_crash)
    v.addWidget(chk_usage)
    row = QHBoxLayout()
    row.addStretch(1)
    btn = QPushButton("确定")
    btn.clicked.connect(dlg.accept)
    row.addWidget(btn)
    v.addLayout(row)
    dlg.exec()
    try:
        if _tc is not None:
            _tc.set_crash_enabled(bool(chk_crash.isChecked()))
        if _tele is not None:
            _tele.set_enabled(bool(chk_usage.isChecked()))
    except Exception:
        pass
    s = _load_settings()
    s["privacy_ack"] = True
    _save_settings(s)


def maybe_run_first_run_wizard(parent=None) -> None:
    """有 manifest 且无环境就位时，弹首启向导。开发机无 manifest 时静默跳过。"""
    if pack_installer is None:
        return
    src = resolve_manifest_source()
    if not src:
        return
    try:
        manifest, src_root = pack_installer.load_manifest(src)
    except Exception:
        return
    if needs_first_run(manifest):
        FirstRunWizard(manifest, src_root, parent).exec()


class _SingleInstance(QObject):
    """单实例守卫：QLockFile 防并发占用 + QLocalServer 让二次启动「激活已有窗口」。

    二次启动时若检测到已有实例，则通过本地 socket 通知其前置窗口，自身随即退出，
    避免叠开多个启动器窗口（此前重复点击启动脚本会出现多窗口）。"""
    activate_requested = Signal()

    def __init__(self, key: str = "AvatarHubLauncher.singleton"):
        super().__init__()
        self._key = key
        self._lock = None
        self._server = None
        self.is_primary = False

    def acquire(self) -> bool:
        """尝试成为主实例。成功=可正常启动；失败=已有实例在跑。"""
        base = QStandardPaths.writableLocation(QStandardPaths.TempLocation) or "."
        self._lock = QLockFile(os.path.join(base, self._key + ".lock"))
        self._lock.setStaleLockTime(0)   # 仍按持有进程存活性判定，进程退出后锁自动失效
        self.is_primary = self._lock.tryLock(80)
        return self.is_primary

    def notify_primary(self) -> bool:
        """二次启动：连到主实例请求显示窗口；成功返回 True。"""
        sock = QLocalSocket()
        sock.connectToServer(self._key)
        if sock.waitForConnected(400):
            try:
                sock.write(b"show")
                sock.flush()
                sock.waitForBytesWritten(400)
                sock.disconnectFromServer()
            except Exception:
                pass
            return True
        return False

    def start_server(self):
        """主实例：监听本地 socket，收到二次启动的请求即发激活信号。"""
        try:
            QLocalServer.removeServer(self._key)   # 清理可能的陈旧 socket
            self._server = QLocalServer(self)
            self._server.newConnection.connect(self._on_conn)
            self._server.listen(self._key)
        except Exception:
            self._server = None

    def _on_conn(self):
        conn = self._server.nextPendingConnection()
        if conn is not None:
            self.activate_requested.emit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("无界幻境 BOUNDLESS Studio")
    # 品牌应用图标：窗口/任务栏统一用 assets\app.ico（∞ 主标深色头像版）。
    # 打包 exe 自带图标，但开发态跑 python 与部分 Qt 窗口场景仍需显式 set。
    try:
        _ico = app_config.BASE / "assets" / "app.ico"
        if _ico.exists():
            app.setWindowIcon(QIcon(str(_ico)))
    except Exception:
        pass
    _init_ui_scale(app)   # 确定全局 UI_SCALE（字号/尺寸分档），供后续构建窗口时引用
    _init_motion()        # 按设置项决定是否启用微交互动效

    # 控制台自身崩溃匿名上报（默认开可关；仅栈签名+脱敏，绝不含内容数据）
    try:
        import telemetry_client as _tc
        _tc.install("launcher")
    except Exception:
        pass

    # exe 摆渡自更新：退出时若有已就绪的新版 exe，spawn 分离摆渡脚本（等本进程退出→替换→重启）。
    try:
        if pack_installer is not None:
            app.aboutToQuit.connect(lambda: pack_installer.spawn_exe_swap_on_exit(log=lambda m: None))
    except Exception:
        pass

    selftest = os.environ.get("AVATARHUB_SELFTEST") == "1"

    # 安装目录可写性体检：装进 Program Files 等只读目录时（旧安装包允许"为所有用户安装"），
    # 组件下载/日志/配置全会失败。这里给出人话指引后退出，而不是让用户看一串异常天书。
    if not selftest:
        try:
            _probe = app_config.BASE / ".write_probe"
            _probe.write_text("", encoding="utf-8")
            _probe.unlink()
        except OSError:
            QMessageBox.critical(
                None, "安装目录不可写",
                f"当前安装位置没有写入权限：\n{app_config.BASE}\n\n"
                "本程序需要在安装目录旁下载模型与写日志。\n"
                "请先在「设置 → 应用」卸载本程序，然后重新运行安装包\n"
                "（新版安装包会自动装到你的用户目录，无需管理员）。")
            return

    # 单实例：非自检模式下，二次启动只激活已有窗口，不再叠开新窗口。
    single = None
    if not selftest:
        single = _SingleInstance()
        if not single.acquire():
            single.notify_primary()
            return

    if not selftest:
        # 启动早期（服务未拉起=安全窗口）先做两件事：
        #  ① 上代热修若"起不来"（上次启动未通过运行验收）→ 自动回滚上一代（自愈）；
        #  ② 应用上次暂存的程序更新（直播中下载的 app 更新转 pending，此刻原子覆盖+可回滚）。
        try:
            if pack_installer is not None:
                pack_installer.check_and_revert_probation(log=lambda m: None)
                pack_installer.apply_pending_app(log=lambda m: None)
        except Exception:
            pass
        # 首启隐私同意页（只一次）→ 首次运行安装向导 → 主控台。
        maybe_show_privacy_consent()
        maybe_run_first_run_wizard()

    w = Launcher()
    if single is not None:
        single.start_server()

        def _bring_to_front():
            w.showNormal()
            w.raise_()
            w.activateWindow()

        single.activate_requested.connect(_bring_to_front)
    # 启动即最小化到托盘：开机自启(--minimized) 或 设置项；无托盘时降级为普通最小化。
    start_min = (not selftest) and (("--minimized" in sys.argv)
                                    or bool(_load_settings().get("start_minimized")))
    if start_min and getattr(w, "tray", None) is not None:
        w.hide()
    elif start_min:
        w.showMinimized()
    else:
        w.show()
    if not selftest:
        w.notify_updates_async()  # 启动后后台检查组件更新
        # 存活心跳（opt-in，一天一条限频）：覆盖"只开控制台不常跑 hub"的用户，DAU 更准。
        try:
            import telemetry_client as _tc
            threading.Thread(target=lambda: _tc.heartbeat("launcher"), daemon=True).start()
        except Exception:
            pass
        # 装完即用：环境就位且非最小化启动时，自动打开网页控制台 /ui（无边框应用窗口，
        # 与浏览器打开的 http://127.0.0.1:9000/ui 完全一致）——让用户开箱看到的就是控制台，
        # 而不是另一套原生界面（Hub 未起则 on_open_ui 会先拉核心链路再打开）。
        # 门槛用 _runtime_envs_installed（本产品自装环境）而非 _core_envs_present：
        # 自动 start_all 会清理被占端口，只有「我们装的单机」才保证无生产服务可误杀。
        # 关闭：launcher_settings.json 置 "auto_open_ui": false。
        will_open_ui = (not start_min) and _load_settings().get("auto_open_ui", True) \
            and (_hub_alive() or _runtime_envs_installed())
        if will_open_ui:
            QTimer.singleShot(600, w.on_open_ui)
        else:
            # 本次不会拉起/校验 hub（最小化托盘或关了自动打开）→ 无从做运行验收，
            # 直接确认 probation，避免下次启动把没验证过的更新误判为"起不来"而回滚。
            try:
                if pack_installer is not None:
                    pack_installer.confirm_app_ok(log=lambda m: None)
            except Exception:
                pass

    # 自检模式（打包/CI 验证用）：轮询一次后把结果写入文件并退出，不弹常驻窗口。
    # 同时探测打包关键依赖（pack_installer / license / cryptography），让冻结后的 exe
    # 能验证这些惰性/条件导入是否被正确打进包（否则首启向导/授权在成品里会失效）。
    if selftest:
        def _finish():
            deps = []
            deps.append("pack_installer=" + ("ok" if pack_installer is not None else "MISSING"))
            try:
                import license as _lic  # noqa: F401
                deps.append("license=ok")
            except Exception as e:
                deps.append(f"license=ERR:{e}")
            try:
                import cryptography  # noqa: F401
                deps.append("cryptography=ok")
            except Exception:
                deps.append("cryptography=MISSING")
            # https 必须真的能用：conda 打包最易踩「libssl 没进包」（DLL 在 Library\bin，
            # PyInstaller 看不到）——开发机 PATH 有 conda 会掩盖，成品机上向导全量 URLError。
            # 故自检直连一次 https（1.0.4 首发在 198 实锤过一回，此检永久看门）。
            try:
                import ssl as _ssl
                from urllib.request import urlopen as _uo
                with _uo("https://usdt2026.cc/releases/release_manifest.json",
                         timeout=8) as _r:
                    deps.append(f"https={_r.status}")
            except Exception as e:
                deps.append(f"https=ERR:{type(e).__name__}")
            try:
                (app_config.BASE / "selftest_result.txt").write_text(
                    f"ok summary={w.badge.text()}\ndeps: {'; '.join(deps)}\n", encoding="utf-8")
            finally:
                app.quit()
        QTimer.singleShot(4500, _finish)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
