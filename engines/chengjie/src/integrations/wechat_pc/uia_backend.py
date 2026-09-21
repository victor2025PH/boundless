# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 真机 UIAutomation 后端（实施97 线 B，Windows 专属）。

锚点全部来自 **2026-09-08 微信 4.1.12.55 真机探针**（``scripts/wechat_pc_probe*.py`` 产物）：

- 主窗 UIA 类 ``mmui::MainWindow``（Win32 类 ``Qt51514QWindowIcon``，标题「微信」）；登录窗 ``mmui::LoginWindow``。
- 会话列表 ``AutomationId=session_list``（``mmui::XTableView``）；会话格 ``mmui::ChatSessionCell``，
  Name＝「显示名\\n预览\\n时间\\n」，AutomationId＝``session_item_<显示名>``（**同名不同号时完全相同**——
  身份只能靠资料卡微信号区分）。预览行以「[N条]」开头＝多条未读；单条未读**没有**任何 UIA 标记，
  故未读用「Name 变化」探测（会话格 Name 随新消息变化）。
- 聊天标题 ``AutomationId`` 含 ``current_chat_name_label``；对方输入中 ``typing_status_view``（rect 非零即在输入）。
- 消息列表 ``AutomationId=chat_message_list``（``mmui::RecyclerListView``，只渲染可见项）；文本气泡
  ``mmui::ChatTextItemView``（Name＝正文）、时间/系统条 ``mmui::ChatItemView``；气泡节点**无方向属性、无子节点**，
  己方/对方只能按视觉判定：整窗 ``PrintWindow`` 自截图（被遮挡也正确，见 ``capture.py``）→ 头像在左＝对方、在右＝己方
  （:func:`classify_bubble_direction`，主题无关；实测用户是深色主题）→ 兜底主题色规则 :func:`bubble_direction_by_pixels`。
- 输入框 ``AutomationId=chat_input_field``（``mmui::ChatInputField``，ValuePattern 可读写）；发送按钮 Name「发送」
  （``mmui::XOutlineButton``）。
- 资料卡：右上「聊天信息」按钮**切换**侧栏 → 侧栏头像 ``mmui::ContactHeadView`` → 弹窗 ``mmui::ProfileUniquePop``
  （Win32 类 ``Qt51514QWindowToolSaveBits``），其中「微信号：」键的下一个 ``mmui::ProfileTextView`` 即微信号；
  弹窗用 WindowPattern.Close 关闭。**绝不发 Esc**：4.x 默认「Esc 关闭窗口」会把主窗藏进托盘（实锤）。
- 找窗口用 Win32 ``EnumWindows``（``win32_windows``）：UIA 根枚举一次 60–90 秒，不可用于每 tick。

安全：``self_check`` 不过（缺必需锚点/无主窗）恒只读；所有方法绝不抛。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.wechat_pc.backend import Bubble, ScreenState, SessionRow
from src.integrations.wechat_pc.identity import normalize_display_name, parse_wxid
from src.integrations.wechat_pc.risk_screens import LOGIN_WINDOW_CLASSES

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 环境相关
    import uiautomation as _uia  # type: ignore
    _UIA_OK = True
except Exception:  # pragma: no cover
    _uia = None
    _UIA_OK = False

UIA_OP_TIMEOUT = 8.0
MAIN_WINDOW_CLASS = "mmui::MainWindow"
PROFILE_POPUP_CLASS = "mmui::ProfileUniquePop"

#: 锚点表（key → 查找规格；``aid``/``class`` 为「包含」匹配候选，``name`` 精确候选）
ANCHORS: Dict[str, Dict[str, Any]] = {
    "login_window": {"class": ["mmui::LoginWindow"]},
    "main_window": {"class": [MAIN_WINDOW_CLASS]},
    "session_list": {"aid_exact": ["session_list"], "type": "ListControl"},
    "session_item": {"class": ["mmui::ChatSessionCell"], "type": "ListItemControl"},
    "chat_title": {"aid": ["current_chat_name_label"], "type": "TextControl"},
    "typing_status": {"aid": ["typing_status_view"]},
    "message_list": {"aid_exact": ["chat_message_list"], "type": "ListControl"},
    "composer": {"aid_exact": ["chat_input_field"], "type": "EditControl"},
    "send_button": {"name": ["发送", "Send"], "type": "ButtonControl"},
    "chat_info_button": {"name": ["聊天信息", "Chat Info"], "type": "ButtonControl"},
    "contact_head": {"class": ["mmui::ContactHeadView"], "type": "ButtonControl"},
    "profile_popup": {"class": [PROFILE_POPUP_CLASS]},
    "profile_wxid_key": {"name": ["微信号：", "微信号:", "WeChat ID:", "WeChat ID："], "type": "TextControl"},
    "profile_value": {"class": ["mmui::ProfileTextView"], "type": "TextControl"},
    # 左侧导航栏本人头像（点开是本人资料卡）：类名/aid 随版本变，按包含匹配；不中时回落几何（会话列表左侧最上方的按钮）
    "self_avatar": {"class": ["Avatar", "HeadImage", "HeadView", "Portrait"], "type": "ButtonControl"},
    # ── 语音（2026-09-19 微信 4.1.13 真机探针）──
    # 输入区右侧工具栏 aid ``tool_bar_accessible.chatinput_toolbar_right_view``（``mmui::ChatInputToolbarRightView``），
    # 闲态含「发语音 ( 按住右 Alt )」（``mmui::XButton``，Name 带热键文案随语言/版本变 → 按前缀匹配，
    # 兜底取工具栏里除「发送」外的 XButton）与「发送」（``mmui::XOutlineButton``）。
    # 点「发语音」后工具栏被 ``mmui::ChatVoiceRecordView`` 替换（**类名与语言无关＝录音态最稳的判据**），
    # 其内「取消」（XButton）、「发送语音」（``mmui::XMouseEventView`` ButtonControl）、波形 aid ``wave view``。
    # 点「取消」/「发送语音」后 ChatVoiceRecordView 消失、工具栏恢复闲态。
    # fast_class：只给 provider 侧 FindFirst 用的精确类名（_matches 不看它；命中后仍按 aid 复核，版本变了回落遍历）
    "voice_toolbar": {"aid": ["chatinput_toolbar_right_view"], "fast_class": ["mmui::ChatInputToolbarRightView"]},
    "voice_button": {"name_prefix": ["发语音", "發語音", "Voice", "Hold"], "type": "ButtonControl"},
    "voice_record_view": {"class": ["mmui::ChatVoiceRecordView"]},
    "voice_cancel": {"name": ["取消", "Cancel"], "type": "ButtonControl"},
    "voice_send": {"name": ["发送语音", "發送語音", "Send Voice", "Send voice", "Send Voice Message", "Send"],
                   "type": "ButtonControl"},
}
REQUIRED_FOR_READ = ("main_window", "session_list")
#: 发送必需：会话列表 + 消息列表 + 输入框 + 标题（消息列表/输入框/标题只有打开会话后才存在——
#: self_check 会先点开列表第一项再判）
REQUIRED_FOR_SEND = ("main_window", "session_list", "message_list", "composer", "chat_title", "send_button")
#: 语音必需（可选能力：缺了只是 voice_ready=False，不影响文字收发；4.1.9 以下客户端没有「发语音」按钮）
REQUIRED_FOR_VOICE = ("voice_toolbar", "voice_button")
#: 录音态确认 / 退出的等待上限（点按后 UI 切换约 0.3–0.9s）
VOICE_STATE_TIMEOUT = 3.0
#: 录音态相关点按的点后等待（uiautomation 默认 0.5s；录音期间点后等待都是录进去的静音）
CLICK_WAIT_SEC = 0.05
#: 播放期间缓存的「发送语音」控件最多用多久（微信语音硬顶 60s；超过说明不是这一条的录音态）
PRIMED_CONTROL_TTL_SEC = 90.0
#: 快速点按（缓存控件、不平滑移动）后等录音态退出的窗口；没退出就回落「重新定位 + 平滑点按」
PRIMED_CLICK_CONFIRM_SEC = 1.0
#: UIA TreeScope_Descendants（uiautomation 包没导出这个枚举）
_TREESCOPE_DESCENDANTS = 0x4
#: 证明 provider 侧 FindFirst 在当前主窗可用的探针类名（正常态主窗必有 QWidget 子树 / 聊天主视图）
FAST_FIND_PROBE_CLASSES = ("mmui::ChatMasterView", "QWidget")

#: 气泡类名 → 收件箱 media 类别（未知 Chat*ItemView 一律 unknown，正文取 Name）
_BUBBLE_KINDS = (
    ("ChatTextItemView", "text"), ("ChatImageItemView", "image"), ("ChatVoiceItemView", "voice"),
    ("ChatFileItemView", "file"), ("ChatVideoItemView", "video"), ("ChatEmojiItemView", "sticker"),
    ("ChatEmoticonItemView", "sticker"), ("ChatLinkItemView", "link"), ("ChatCardItemView", "card"),
    ("ChatLocationItemView", "location"), ("ChatSystemItemView", "system"), ("ChatItemView", "system"),
)


#: 微信占位文本 → 类别（类名认不出时的兜底；预览行/气泡 Name 都会用这些占位）
_PLACEHOLDER_KINDS = (
    ("[图片]", "image"), ("[Photo]", "image"), ("[视频]", "video"), ("[Video]", "video"),
    ("[语音]", "voice"), ("[Voice]", "voice"), ("[Audio]", "voice"), ("[文件]", "file"), ("[File]", "file"),
    ("[动画表情]", "sticker"), ("[表情]", "sticker"), ("[Sticker]", "sticker"), ("[位置]", "location"),
    ("[Location]", "location"), ("[链接]", "link"), ("[Link]", "link"), ("[名片]", "card"), ("[Contact Card]", "card"),
    ("[转账]", "transfer"), ("[Transfer]", "transfer"), ("[红包]", "redpacket"), ("[Red Packet]", "redpacket"),
    ("[小程序]", "miniapp"), ("[Mini Program]", "miniapp"), ("[聊天记录]", "history"), ("[Chat History]", "history"),
    ("[音乐]", "link"), ("[Music]", "link"), ("[视频号]", "link"), ("[Channels]", "link"),
)


def bubble_kind_for(class_name: str, name: str) -> str:
    """气泡类别：先按 UIA 类名（``Chat<Kind>ItemView``），认不出再按微信占位文本（「[图片]」等）；都不是 → text
    （有正文）或 unknown（无正文）。纯函数。"""
    cls = str(class_name or "")
    for marker, k in _BUBBLE_KINDS:
        if marker in cls:
            return k
    s = str(name or "").strip()
    for token, k in _PLACEHOLDER_KINDS:
        if s.startswith(token):
            return k
    return "text" if s else "unknown"


def available() -> bool:
    return bool(_UIA_OK and os.name == "nt")


def _s(v: Any) -> str:
    return str(v if v is not None else "")


def _matches(ctrl: Any, spec: Dict[str, Any]) -> bool:
    try:
        if spec.get("type") and getattr(ctrl, "ControlTypeName", "") != spec["type"]:
            return False
        cls = _s(getattr(ctrl, "ClassName", ""))
        aid = _s(getattr(ctrl, "AutomationId", ""))
        name = _s(getattr(ctrl, "Name", ""))
        if spec.get("class") and not any(c in cls for c in spec["class"]):
            return False
        if spec.get("aid_exact") and aid not in spec["aid_exact"]:
            return False
        if spec.get("aid") and not any(a in aid for a in spec["aid"]):
            return False
        if spec.get("name") and name not in spec["name"]:
            return False
        if spec.get("name_prefix") and not any(name.startswith(p) for p in spec["name_prefix"]):
            return False
        return True
    except Exception:
        return False


def _walk(ctrl: Any, max_depth: int = 26):
    stack = [(ctrl, 0)]
    while stack:
        c, d = stack.pop()
        yield c
        if d >= max_depth:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            stack.append((k, d + 1))


def _find_first_by_class(root: Any, class_names: List[str]) -> Optional[Any]:
    """provider 侧原生 ``FindFirst(Descendants, ClassName==x)``：一趟 COM 调用，比 :func:`_walk` 逐节点跨进程
    读属性快一个量级（真机 mmui 主窗全树 miss ≈250ms → ≈10ms）。录音态确认要每 100ms 轮询一次，走这条。

    只认**精确**类名；返回 None 既可能是「没有」也可能是「不支持」——调用方自行决定是否回落 walk。
    """
    if not available() or root is None:
        return None
    try:
        import uiautomation as auto  # type: ignore
        import uiautomation.uiautomation as _impl  # type: ignore
        client = _impl._AutomationClient.instance().IUIAutomation  # noqa: SLF001
        el = getattr(root, "Element", None)
        if el is None:
            return None
        for cls in class_names:
            cond = client.CreatePropertyCondition(auto.PropertyId.ClassNameProperty, cls)
            hit = el.FindFirst(_TREESCOPE_DESCENDANTS, cond)
            if hit:   # NULL 指针为假
                return auto.Control.CreateControlFromElement(hit)
    except Exception:
        logger.debug("[wechat_pc.uia] FindFirst(ClassName) 不可用", exc_info=True)
    return None


def parse_session_cell_name(raw: str) -> Tuple[str, str, str, int]:
    """会话格 Name「显示名\\n预览\\n时间\\n」→ (显示名, 预览, 时间, 多条未读数)。

    多条未读时预览行以「[N条] 」开头（单条未读无标记）。时间行形如「02:27 / 昨天 / 星期三 / 9/1」。
    """
    lines = [ln.strip() for ln in _s(raw).split("\n")]
    name = lines[0] if lines else ""
    preview = lines[1] if len(lines) > 1 else ""
    tm = lines[2] if len(lines) > 2 else ""
    unread = 0
    if preview.startswith("[") and "条]" in preview[:8]:
        num = preview[1:preview.index("条]")]
        if num.isdigit():
            unread = int(num)
        rest = preview.split("条]", 1)[1].strip()
        # 4.x 把「[4条] 」单独占一行时预览在第 3 行、时间在第 4 行
        if not rest and len(lines) > 3:
            preview, tm = lines[2], lines[3]
        else:
            preview = rest
    return name, preview, tm, unread


_CN_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_time_label(label: str, now: Optional[float] = None) -> float:
    """微信消息列表时间条 → epoch 秒（分钟精度）。认不出返回 0。

    形态（4.x 实录/常见）：``02:27``、``昨天 02:27``、``星期三 02:27``、``9月1日 02:27``、
    ``2025年12月31日 02:27``、``2026/9/1 02:27``。相对日期以本机当天为基准。
    """
    import datetime as _dt
    import re as _re
    s = " ".join(str(label or "").split())
    if not s:
        return 0.0
    base = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    m = _re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return 0.0
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return 0.0
    day = base.date()
    head = s[:m.start()].strip()
    try:
        if not head:
            pass
        elif head.startswith("昨天"):
            day = day - _dt.timedelta(days=1)
        elif head.startswith("前天"):
            day = day - _dt.timedelta(days=2)
        elif head.startswith("星期") and len(head) >= 3 and head[2] in _CN_WEEKDAYS:
            target = _CN_WEEKDAYS[head[2]]
            delta = (day.weekday() - target) % 7
            day = day - _dt.timedelta(days=delta or 7)
        else:
            m2 = _re.match(r"(?:(\d{4})[年/\-])?(\d{1,2})[月/\-](\d{1,2})日?", head)
            if m2:
                year = int(m2.group(1)) if m2.group(1) else day.year
                day = _dt.date(year, int(m2.group(2)), int(m2.group(3)))
            else:
                return 0.0
    except Exception:
        return 0.0
    return _dt.datetime(day.year, day.month, day.day, hh, mm).timestamp()


def _is_wechat_green(argb: int) -> bool:
    r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
    # 浅色 #95EC69(149,236,105) / 深色约 (62,181,117)：绿分量显著高于红蓝
    return g > 140 and g - r > 35 and g - b > 45


def _is_background(argb: int) -> bool:
    r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
    # 浅色底 #F2F2F2（对方白泡 #FFFFFF 更亮，不算底）/ 深色底 #191919（对方泡 #2C2C2C 更亮，不算底）
    neutral = abs(r - g) < 8 and abs(g - b) < 8
    return neutral and (225 <= r <= 250 or r <= 30)


def backfill_ts_hints(bubbles: List[Bubble]) -> List[Bubble]:
    """可见区顶部（第一条时间条之前）的气泡没有时间：用其**下方最近一条时间条的时间 − 1 秒**做上界——它们一定早于
    那条时间条；比按「现在」入站诚实得多（真机实锤：聊天变长后旧消息被打上新时间）。没有任何时间条 → 保持 0。原地改、纯函数语义。"""
    next_label = 0.0
    for b in reversed(bubbles):
        if b.kind == "system":
            if b.ts_hint:
                next_label = b.ts_hint
            continue
        if not b.ts_hint and next_label:
            b.ts_hint = next_label - 1.0
            b.ts_is_upper_bound = True
    return bubbles


def compute_unread_flags(raws: List[str], last_raw_names: set, bootstrap_top_n: int) -> List[int]:
    """每个会话格的「待读」数（纯函数）。

    - 预览行带「[N条]」→ N；
    - 否则：非首轮且该格原始 Name 不在上一轮集合里（新消息改了预览/时间）→ 1。按**内容集合**而非位置
      对比：新消息会把会话顶到第一位，按位置记基线会让新顶上来的格没有基线、被顶下去的格误报；
    - 首轮（集合为空）：最近 ``bootstrap_top_n`` 个有预览的非系统会话 → 1（历史引导读取）。
    """
    from src.integrations.wechat_pc.policy import is_system_session
    first_scan = not last_raw_names
    out: List[int] = []
    bootstrapped = 0
    for raw in raws:
        name, preview, _tm, unread = parse_session_cell_name(raw)
        if not name:
            out.append(0)
            continue
        if unread == 0 and not first_scan and raw not in last_raw_names and preview:
            unread = 1
        if (unread == 0 and first_scan and preview and bootstrapped < bootstrap_top_n
                and not is_system_session(name)):
            unread = 1
            bootstrapped += 1
        out.append(unread)
    return out


def bubble_direction_by_pixels(pixels_right: List[int], pixels_left: List[int]) -> Optional[bool]:
    """（兜底规则，依赖主题色）右侧采样含微信绿 → 己方(True)；左侧有非背景像素（白/灰泡）→ 对方(False)；都没有 → None。"""
    if any(_is_wechat_green(p) for p in pixels_right):
        return True
    if any(not _is_background(p) for p in pixels_left):
        return False
    return None


def _differs(p: int, ref: int, tol: int = 12) -> bool:
    return (abs(((p >> 16) & 0xFF) - ((ref >> 16) & 0xFF)) > tol
            or abs(((p >> 8) & 0xFF) - ((ref >> 8) & 0xFF)) > tol
            or abs((p & 0xFF) - (ref & 0xFF)) > tol)


def background_reference(samples: List[int]) -> Optional[int]:
    """行上边缘采样的众数（行与行之间有留白，上边缘一律是列表背景；任何主题都成立）。"""
    if not samples:
        return None
    counts: Dict[int, int] = {}
    for p in samples:
        counts[p] = counts.get(p, 0) + 1
    ref, n = max(counts.items(), key=lambda kv: kv[1])
    return ref if n * 2 >= len(samples) else None


def classify_bubble_direction(ref: Optional[int], left_zone: List[int], right_zone: List[int],
                              inner_right: List[int], inner_left: List[int]) -> Optional[bool]:
    """与主题无关的判向（纯函数）：**头像在哪边**。

    真机量测（4.1.12.55，深色主题，行宽 576）：对方行头像 x≈4–8%、气泡自 12% 起；己方行气泡 34–78%、头像 92–96%；
    气泡永远碰不到对侧头像区。于是：左头像区有非背景像素且右头像区干净 → 对方；反之 → 己方。
    参考色取不到或两侧都脏/都干净 → 退回主题色规则 :func:`bubble_direction_by_pixels`。
    """
    if ref is not None and left_zone and right_zone:
        ln = sum(1 for p in left_zone if _differs(p, ref))
        rn = sum(1 for p in right_zone if _differs(p, ref))
        if ln >= 3 and rn <= 1:
            return False
        if rn >= 3 and ln <= 1:
            return True
    return bubble_direction_by_pixels(inner_right, inner_left)


class UiaBackend:
    """真机后端。构造不触碰 UIA；首次调用时枚举。"""

    def __init__(self, anchors: Optional[Dict[str, Dict[str, Any]]] = None, *,
                 hwnd: int = 0, pid: int = 0) -> None:
        self.anchors = dict(ANCHORS)
        if anchors:
            for k, v in anchors.items():
                if isinstance(v, dict):
                    self.anchors[k] = {**self.anchors.get(k, {}), **v}
        self.missing_anchors: List[str] = []
        self.readonly = True
        # 上一轮全部会话格的原始 Name 集合（按内容而非位置：新消息会把会话顶到第一位，
        # 按位置记基线时新顶上来的格没有基线、被顶下去的格反而误报——真机实锤）
        self._last_raw_names: set = set()
        self._main_hwnd = 0
        # 窗口绑定（2026-09-21 P0-2，双开微信串号事故）：一个驱动实例只看**一个** weixin.exe 的窗口。
        # ``bind_hwnd``/``bind_pid`` 是启动时显式绑的（不命中就当没窗，绝不换号）；都没绑时首次看到主窗就锤定它的
        # pid（``_main_pid``），之后另一个微信启动/枚举次序变化都不会把它带到别人的窗口上；那个进程没了（微信重启）才重新锚定。
        self.bind_hwnd = int(hwnd or 0)
        self.bind_pid = int(pid or 0)
        self._main_pid = 0
        self.main_window_count = 0
        self._last_check = 0.0
        # 播放期间提前定位到的「发送语音」按钮（控件, 定位时刻）；finish_voice_record 先点它，省掉播完后的 UIA 遍历
        self._primed_voice_send: Optional[Tuple[Any, float]] = None
        # provider 侧 FindFirst 在本进程里命中过录音态视图 → 之后 miss 不再回落全树遍历
        self._fast_find_proven = False

    # ── 窗口发现（Win32 → UIA） ──
    @property
    def bound(self) -> bool:
        """启动时显式绑了窗口/进程（双开时没绑 = 靠首见锚定，工作台要提醒主人绑一下）。"""
        return bool(self.bind_hwnd or self.bind_pid)

    def _target_pid(self, wins: List[Any]) -> int:
        """这轮枚举里本实例该看的进程；0 = 不限（还没主窗，如只有登录窗）；-1 = 绑了但目标不在（什么都不看）。"""
        from src.integrations.wechat_pc.win32_windows import main_windows
        mains = main_windows(wins)
        self.main_window_count = len(mains)
        if self.bind_hwnd:
            hit = next((w for w in wins if w.hwnd == self.bind_hwnd), None)
            if hit is not None:
                self.bind_pid = self.bind_pid or hit.pid   # 句柄会随退登/重登换，进程不会：首次命中就记下 pid
                return hit.pid
            if not self.bind_pid:
                return -1
        if self.bind_pid:
            return self.bind_pid
        if self._main_pid and any(w.pid == self._main_pid for w in wins):
            return self._main_pid
        self._main_pid = mains[0].pid if mains else 0
        return self._main_pid

    def _bound_windows(self) -> List[Any]:
        """可见的微信顶层窗（:class:`TopWindow`），已按绑定过滤到单个进程。"""
        try:
            from src.integrations.wechat_pc.win32_windows import find_wechat_windows
        except Exception:
            return []
        wins = find_wechat_windows(visible_only=True)
        pid = self._target_pid(wins)
        if pid < 0:
            return []
        if pid:
            wins = [w for w in wins if w.pid == pid]
        return wins

    def _qt_windows(self) -> List[Any]:
        if not available():
            return []
        out: List[Any] = []
        for w in self._bound_windows():
            if not w.class_name.startswith("Qt"):
                continue
            try:
                c = _uia.ControlFromHandle(w.hwnd)
                if c is not None:
                    out.append(c)
            except Exception:
                continue
        return out

    def _main_window(self) -> Optional[Any]:
        for c in self._qt_windows():
            if _s(getattr(c, "ClassName", "")) == MAIN_WINDOW_CLASS:
                try:
                    self._main_hwnd = int(c.NativeWindowHandle)
                except Exception:
                    pass
                return c
        return None

    def main_pid(self) -> int:
        """当前盯的 weixin.exe 进程号（没有 → 0）；心跳带给工作台分账号用。"""
        return int(self.bind_pid or self._main_pid or 0)

    def window_binding(self) -> Dict[str, Any]:
        return {"window_hwnd": int(self._main_hwnd or 0), "window_pid": self.main_pid(),
                "window_bound": self.bound, "main_windows": int(self.main_window_count)}

    def _find(self, root: Any, key: str, limit: int = 1, max_depth: int = 26) -> List[Any]:
        spec = self.anchors.get(key) or {}
        found: List[Any] = []
        deadline = time.monotonic() + UIA_OP_TIMEOUT
        for c in _walk(root, max_depth):
            if time.monotonic() > deadline:
                break
            if _matches(c, spec):
                found.append(c)
                if len(found) >= limit:
                    break
        return found

    @staticmethod
    def _activate(win: Any) -> None:
        try:
            win.SetActive()
            time.sleep(0.25)
        except Exception:
            pass

    # ── 自检 ──
    def self_check(self) -> Dict[str, Any]:
        rep: Dict[str, Any] = {"uia": available(), "window": False, "logged_in": False,
                               "missing": [], "readonly": True, "opened_first_session": False}
        if not available():
            self.readonly = True
            return rep
        win = self._main_window()
        rep["window"] = win is not None
        rep.update(self.window_binding())
        if win is None:
            self.missing_anchors = list(REQUIRED_FOR_SEND)
            rep["missing"] = self.missing_anchors
            self.readonly = True
            return rep
        rep["logged_in"] = True
        missing = [k for k in REQUIRED_FOR_READ if not self._find(win, k)]
        # 聊天区锚点要打开一个会话才存在：没有打开时点列表第一项（等同用户切会话，只读语义）
        if not missing and not self._find(win, "message_list"):
            cells = self._session_cells(win)
            if cells:
                self._activate(win)
                try:
                    cells[0].Click(simulateMove=True)
                    time.sleep(1.2)
                    rep["opened_first_session"] = True
                except Exception:
                    pass
        missing += [k for k in REQUIRED_FOR_SEND if k not in REQUIRED_FOR_READ and not self._find(win, k)]
        self.missing_anchors = missing
        self.readonly = bool(missing)
        rep["missing"] = missing
        rep["readonly"] = self.readonly
        # 语音是可选能力：只报状态不锁只读（老版本微信没有「发语音」按钮照常收发文字）
        rep["voice_ready"] = bool(not missing and self._voice_button(win) is not None)
        rep["voice_missing"] = [] if rep["voice_ready"] else [
            k for k in REQUIRED_FOR_VOICE if not self._find(win, k)]
        self._last_check = time.time()
        return rep

    # ── 协议实现 ──
    def screen_state(self) -> ScreenState:
        wins = self._qt_windows()
        if not wins:
            return ScreenState(False)
        login = [w for w in wins if _s(getattr(w, "ClassName", "")) in LOGIN_WINDOW_CLASSES]
        main = next((w for w in wins if _s(getattr(w, "ClassName", "")) == MAIN_WINDOW_CLASS), None)
        texts: List[str] = []
        try:
            for w in wins:
                if w is main:
                    continue
                for c in _walk(w, max_depth=10):
                    if getattr(c, "ControlTypeName", "") in ("TextControl", "ButtonControl"):
                        nm = _s(getattr(c, "Name", ""))
                        if nm:
                            texts.append(nm)
                    if len(texts) > 80:
                        break
        except Exception:
            pass
        title = self._title_text(main) if main is not None else ""
        if login and main is None:
            wc = _s(getattr(login[0], "ClassName", ""))
        else:
            wc = _s(getattr(main, "ClassName", "")) if main is not None else ""
        return ScreenState(True, wc, main is not None and not login, texts, title)

    def _session_cells(self, win: Any) -> List[Any]:
        lists = self._find(win, "session_list")
        if not lists:
            return []
        try:
            return [c for c in lists[0].GetChildren() if _matches(c, self.anchors["session_item"])]
        except Exception:
            return []

    #: 首轮（无变化基线）把最近 N 个非系统会话当「有新消息」打开一次做历史引导——单条未读在 4.x
    #: 没有任何 UIA 标记，否则启动前到达的消息永远读不到（真机实锤：两个客户各一条未读全被漏掉）
    BOOTSTRAP_TOP_N = 5

    def list_sessions(self, limit: int = 50) -> List[SessionRow]:
        main = self._main_window()
        if main is None:
            return []
        raws = [_s(getattr(cell, "Name", "")) for cell in self._session_cells(main)[:limit]]
        flags = compute_unread_flags(raws, self._last_raw_names, self.BOOTSTRAP_TOP_N)
        rows: List[SessionRow] = []
        for idx, raw in enumerate(raws):
            name, preview, _tm, _u = parse_session_cell_name(raw)
            if not name:
                continue
            rows.append(SessionRow(normalize_display_name(name), unread=flags[idx], preview=preview,
                                   is_group=False, raw_name=raw, index=idx))
        if any(raws):
            self._last_raw_names = set(r for r in raws if r)
        return rows

    def open_session(self, display_name: str, *, expected_wxid: str = "", index: int = -1) -> bool:
        """点开会话格。同名多格时：优先 ``index``；给了 ``expected_wxid`` 则逐格核对资料卡微信号。"""
        main = self._main_window()
        if main is None:
            return False
        cells = self._session_cells(main)
        want = normalize_display_name(display_name)
        candidates = [c for c in cells
                      if normalize_display_name(parse_session_cell_name(_s(c.Name))[0]) == want]
        if 0 <= index < len(cells):
            c = cells[index]
            if normalize_display_name(parse_session_cell_name(_s(c.Name))[0]) == want:
                candidates = [c] + [x for x in candidates if x is not c]
        if not candidates:
            return False
        if len(candidates) > 1 and index < 0:
            # 同名多格且没指定格：**已选中**的那格排最前——它就是当前打开的会话（刚核对过身份、刚发过上一条
            # 分条的那个人）；否则按列表位置先点第一格会把会话切到同名的另一个人身上
            sel = [c for c in candidates if self._cell_selected(c)]
            if sel:
                candidates = sel + [x for x in candidates if x not in sel]
        self._activate(main)
        for cell in candidates:
            try:
                # 4.x 实锤：点击**已选中**的会话格会把聊天面板收起（再点一次才恢复）——已选中的直接用；
                # 面板若已被收起（无标题/输入框）则点一下恢复
                if self._cell_selected(cell):
                    if not self._find(main, "composer"):
                        cell.Click(simulateMove=True)
                        time.sleep(0.9)
                else:
                    cell.Click(simulateMove=True)
                    time.sleep(0.9)
            except Exception:
                continue
            if not expected_wxid:
                return True
            got = self.read_profile_wxid()
            if got and got == expected_wxid:
                return True
            if len(candidates) == 1:
                return bool(got == expected_wxid)
        return False

    @staticmethod
    def _cell_selected(cell: Any) -> bool:
        try:
            sp = cell.GetSelectionItemPattern()
            return bool(sp and sp.IsSelected)
        except Exception:
            return False

    def _title_text(self, main: Any) -> str:
        t = self._find(main, "chat_title")
        return _s(getattr(t[0], "Name", "")) if t else ""

    def current_title(self) -> str:
        main = self._main_window()
        return self._title_text(main) if main is not None else ""

    def main_hwnd(self) -> int:
        """当前主窗句柄（没有 → 0）；service 的无障碍树空壳自愈用。"""
        try:
            hwnd = self._main_hwnd
            if not hwnd:
                main = self._main_window()
                hwnd = self._main_hwnd if main is not None else 0
            return int(hwnd or 0)
        except Exception:
            return 0

    def window_minimized(self) -> bool:
        """主窗最小化（PrintWindow 取不到内容 → 判不了方向 → 消息不入站）。"""
        try:
            from src.integrations.wechat_pc.capture import is_minimized
            hwnd = self._main_hwnd
            if not hwnd:
                main = self._main_window()
                hwnd = self._main_hwnd if main is not None else 0
            return bool(hwnd) and is_minimized(hwnd)
        except Exception:
            return False

    def peer_typing(self) -> bool:
        main = self._main_window()
        if main is None:
            return False
        for c in self._find(main, "typing_status"):
            try:
                r = c.BoundingRectangle
                if (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                    return True
            except Exception:
                pass
        return False

    #: 头像区 / 气泡内区的相对采样位置（见 classify_bubble_direction 的量测依据）
    _ZONE_X_LEFT = tuple(i / 100 for i in range(2, 11))
    _ZONE_X_RIGHT = tuple(i / 100 for i in range(90, 99))
    _INNER_X_RIGHT = (0.62, 0.72, 0.82, 0.88)
    _INNER_X_LEFT = (0.14, 0.2, 0.3, 0.4)
    _ZONE_Y = (0.3, 0.5, 0.7)

    def _grab_window(self, main: Any) -> Optional[Any]:
        """整窗自截图（PrintWindow，被遮挡也正确）；最小化/失败 → None。"""
        try:
            from src.integrations.wechat_pc.capture import WindowCapture
            hwnd = int(getattr(main, "NativeWindowHandle", 0) or self._main_hwnd or 0)
            return WindowCapture.grab(hwnd) if hwnd else None
        except Exception:
            return None

    def _bubble_direction(self, item: Any, cap: Optional[Any]) -> Optional[bool]:
        """一条气泡行的方向：头像在哪边（主题无关）→ 兜底主题色规则 → None（截不到图/判不出）。"""
        if cap is None:
            return None
        try:
            r = item.BoundingRectangle
            l, t, rt, bt = int(r.left), int(r.top), int(r.right), int(r.bottom)
            if rt - l < 80 or bt - t < 8:
                return None
            top_edge = cap.row(t + 1, [l + int((rt - l) * f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)])
            ref = background_reference(top_edge)
            left_zone = cap.sample_rect(l, t, rt, bt, self._ZONE_X_LEFT, self._ZONE_Y)
            right_zone = cap.sample_rect(l, t, rt, bt, self._ZONE_X_RIGHT, self._ZONE_Y)
            inner_right = cap.sample_rect(l, t, rt, bt, self._INNER_X_RIGHT, self._ZONE_Y)
            inner_left = cap.sample_rect(l, t, rt, bt, self._INNER_X_LEFT, self._ZONE_Y)
            return classify_bubble_direction(ref, left_zone, right_zone, inner_right, inner_left)
        except Exception:
            return None

    def read_visible_messages(self) -> List[Bubble]:
        main = self._main_window()
        if main is None:
            return []
        lists = self._find(main, "message_list")
        if not lists:
            return []
        out: List[Bubble] = []
        cap = self._grab_window(main)
        try:
            ts_hint = 0.0
            for idx, item in enumerate(lists[0].GetChildren()):
                if getattr(item, "ControlTypeName", "") != "ListItemControl":
                    continue
                cls = _s(getattr(item, "ClassName", ""))
                text = _s(getattr(item, "Name", ""))
                kind = bubble_kind_for(cls, text)
                rid = ""
                try:
                    rid = ",".join(str(x) for x in (item.GetRuntimeId() or []))
                except Exception:
                    rid = ""
                if kind == "system":
                    # 时间条：给其后的气泡当时间戳（分钟精度；配合内容指纹跨重启去重）
                    t = parse_time_label(text)
                    if t:
                        ts_hint = t
                    out.append(Bubble(text, is_self=False, kind="system", runtime_id=rid, index=idx,
                                      ts_hint=t))
                    continue
                direction = self._bubble_direction(item, cap)
                b = Bubble(text, is_self=bool(direction), kind=kind, runtime_id=rid, index=idx,
                           ts_hint=ts_hint)
                b.direction_known = direction is not None
                out.append(b)
        except Exception:
            logger.debug("[wechat_pc.uia] 读气泡失败", exc_info=True)
        return backfill_ts_hints(out)

    def read_composer(self) -> str:
        main = self._main_window()
        if main is None:
            return ""
        eds = self._find(main, "composer")
        if not eds:
            return ""
        try:
            vp = eds[0].GetValuePattern()
            return _s(vp.Value) if vp else ""
        except Exception:
            return ""

    def set_composer(self, text: str) -> bool:
        if self.readonly:
            return False
        main = self._main_window()
        if main is None:
            return False
        eds = self._find(main, "composer")
        if not eds:
            return False
        try:
            self._activate(main)
            ed = eds[0]
            ed.Click(simulateMove=True)
            time.sleep(0.15)
            ed.SendKeys("{Ctrl}a{Delete}", interval=0.02)
            if text:
                # 剪贴板粘贴：emoji/多行不被输入法吞；拟人的「打字时长」由守卫层用延迟补
                _uia.SetClipboardText(text)
                time.sleep(0.05)
                ed.SendKeys("{Ctrl}v", interval=0.05)
                time.sleep(0.2)
            return True
        except Exception:
            logger.debug("[wechat_pc.uia] set_composer 失败", exc_info=True)
            return False

    def press_send(self) -> bool:
        if self.readonly:
            return False
        main = self._main_window()
        if main is None:
            return False
        try:
            self._activate_for_click(main)
            btns = self._find(main, "send_button")
            if btns and not btns[0].IsOffscreen:
                btns[0].Click(simulateMove=True)
            else:
                eds = self._find(main, "composer")
                if not eds:
                    return False
                eds[0].Click(simulateMove=True)
                time.sleep(0.1)
                eds[0].SendKeys("{Enter}")
            # 点了「发送」不等于发出去了：双开/用户碰鼠标时点击可能落空，按「输入框已清空」判定
            for _ in range(6):
                time.sleep(0.25)
                if not self.read_composer().strip():
                    return True
            eds = self._find(main, "composer")
            if eds:
                eds[0].Click(simulateMove=True)
                time.sleep(0.1)
                eds[0].SendKeys("{Enter}")
                for _ in range(6):
                    time.sleep(0.25)
                    if not self.read_composer().strip():
                        return True
            logger.warning("[wechat_pc.uia] press_send 后输入框仍有内容，视为未发出")
        except Exception:
            logger.debug("[wechat_pc.uia] press_send 失败", exc_info=True)
        return False

    # ── 语音 ──
    def _voice_toolbar(self, main: Any) -> Optional[Any]:
        spec = self.anchors.get("voice_toolbar", {})
        hit = _find_first_by_class(main, list(spec.get("fast_class") or []))
        if hit is not None and _matches(hit, spec):
            return hit
        tbs = self._find(main, "voice_toolbar")
        return tbs[0] if tbs else None

    def _voice_button(self, main: Any) -> Optional[Any]:
        """闲态「发语音」按钮：工具栏内按名字前缀找；找不到兜底取工具栏里不是「发送」的 XButton。"""
        tb = self._voice_toolbar(main)
        if tb is None:
            return None
        hits = self._find(tb, "voice_button", max_depth=6)
        if hits:
            return hits[0]
        send_names = set(self.anchors.get("send_button", {}).get("name") or [])
        for c in _walk(tb, 6):
            try:
                if (getattr(c, "ControlTypeName", "") == "ButtonControl" and "XButton" in _s(c.ClassName)
                        and _s(c.Name) and _s(c.Name) not in send_names):
                    return c
            except Exception:
                continue
        return None

    @staticmethod
    def _visible(c: Any) -> bool:
        try:
            r = c.BoundingRectangle
            return (r.right - r.left) > 0 and (r.bottom - r.top) > 0
        except Exception:
            return True

    def _record_view(self, main: Any) -> Optional[Any]:
        """录音态视图（``mmui::ChatVoiceRecordView``）。先走 provider 侧 FindFirst（真机 ≈15ms，全树遍历 ≈150–400ms）。

        FindFirst 没找到时先用一个**必然存在**的类名（:data:`FAST_FIND_PROBE_CLASSES`）证明这条路在当前窗口上可用：
        能找到探针却找不到录音视图＝确实不在录音态，直接返回，不回落遍历——录音态确认/退出都是 100ms 轮询，
        点下「发语音」之后每一次遍历都是录进去的几百毫秒开头静音。探针也找不到（无障碍树空壳/老版本）才遍历。
        """
        classes = list(self.anchors.get("voice_record_view", {}).get("class") or [])
        hit = _find_first_by_class(main, classes)
        if hit is not None:
            self._fast_find_proven = True
            return hit if self._visible(hit) else None
        if not self._fast_find_proven and _find_first_by_class(main, list(FAST_FIND_PROBE_CLASSES)) is not None:
            self._fast_find_proven = True
        if self._fast_find_proven:
            return None
        for c in self._find(main, "voice_record_view"):
            if self._visible(c):
                return c
        return None

    def _wait_recording(self, main: Any, want: bool, timeout: float = VOICE_STATE_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if (self._record_view(main) is not None) == want:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.1)

    @staticmethod
    def _activate_for_click(win: Any) -> None:
        """点按前置前：主窗已在前台就不等（``_activate`` 固定睡 250ms——录音态里每一毫秒都是录进去的静音）。"""
        try:
            import ctypes
            fg = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            if fg and fg == int(getattr(win, "NativeWindowHandle", 0) or 0):
                return
        except Exception:
            pass
        UiaBackend._activate(win)

    def voice_ready(self) -> bool:
        if self.readonly:
            return False
        main = self._main_window()
        return main is not None and self._voice_button(main) is not None

    def voice_recording(self) -> bool:
        main = self._main_window()
        return main is not None and self._record_view(main) is not None

    def start_voice_record(self) -> bool:
        """点「发语音」并确认进入录音态（ChatVoiceRecordView 出现）。已在录音态直接 True。"""
        if self.readonly:
            return False
        main = self._main_window()
        if main is None:
            return False
        self._primed_voice_send = None
        try:
            if self._record_view(main) is not None:
                return True
            btn = self._voice_button(main)
            if btn is None:
                return False
            self._activate_for_click(main)
            # waitTime≈0：uiautomation 点完默认再睡 0.5s——录音已经开始，这 0.5s 全是录进去的开头静音
            btn.Click(simulateMove=True, waitTime=CLICK_WAIT_SEC)
            return self._wait_recording(main, True)
        except Exception:
            logger.debug("[wechat_pc.uia] start_voice_record 失败", exc_info=True)
            return False

    def _locate_in_record_view(self, main: Any, key: str, *, fallback_named: bool) -> Optional[Any]:
        rv = self._record_view(main)
        if rv is None:
            return None
        hits = self._find(rv, key, max_depth=8)
        if not hits and fallback_named:
            # 英文/新版本名字对不上：录音态里除「取消」外唯一带名字的按钮就是「发送语音」
            cancel_names = set(self.anchors.get("voice_cancel", {}).get("name") or [])
            for c in _walk(rv, 8):
                try:
                    if (getattr(c, "ControlTypeName", "") == "ButtonControl" and _s(c.Name)
                            and _s(c.Name) not in cancel_names):
                        hits = [c]
                        break
                except Exception:
                    continue
        return hits[0] if hits else None

    def _click_in_record_view(self, main: Any, key: str, *, fallback_named: bool) -> bool:
        ctl = self._locate_in_record_view(main, key, fallback_named=fallback_named)
        if ctl is None:
            return False
        self._activate_for_click(main)
        ctl.Click(simulateMove=True, waitTime=CLICK_WAIT_SEC)
        return True

    def prime_voice_send(self) -> bool:
        """录音态里提前定位「发送语音」按钮并缓存（send_guard 在**播放期间**调用，把 UIA 遍历藏进播放窗口）。"""
        self._primed_voice_send = None
        if self.readonly:
            return False
        main = self._main_window()
        if main is None:
            return False
        try:
            ctl = self._locate_in_record_view(main, "voice_send", fallback_named=True)
        except Exception:
            logger.debug("[wechat_pc.uia] prime_voice_send 失败", exc_info=True)
            return False
        if ctl is None:
            return False
        self._primed_voice_send = (ctl, time.monotonic())
        return True

    def finish_voice_record(self) -> bool:
        """点「发送语音」结束录音并发出；确认录音态退出。不在录音态 → False。

        先点播放期间缓存的按钮（不再遍历 UIA；控件失效/过期 → 重新定位），播完到点下去的尾部静音目标 ≤200ms。
        """
        if self.readonly:
            return False
        main = self._main_window()
        if main is None:
            return False
        primed, self._primed_voice_send = self._primed_voice_send, None
        fast_clicked = False
        try:
            if primed is not None and time.monotonic() - primed[1] <= PRIMED_CONTROL_TTL_SEC:
                try:
                    ctl = primed[0]
                    if self._visible(ctl):
                        self._activate_for_click(main)
                        # 直接落点不做平滑移动（平滑移动按距离要 100–300ms，都是录进去的尾部静音）
                        ctl.Click(simulateMove=False, waitTime=CLICK_WAIT_SEC)
                        fast_clicked = True
                        if self._wait_recording(main, False, timeout=PRIMED_CLICK_CONFIRM_SEC):
                            return True
                        logger.debug("[wechat_pc.uia] 快速点发送语音未退出录音态，回落重新定位+平滑点按")
                except Exception:
                    logger.debug("[wechat_pc.uia] 缓存的发送语音按钮失效，重新定位", exc_info=True)
            if not self._click_in_record_view(main, "voice_send", fallback_named=True):
                # 录音视图已经不在：快速点按其实生效、只是退出慢了一步（不能判失败——已发出的语音会被人再发一次）
                return fast_clicked and self._record_view(main) is None
            return self._wait_recording(main, False)
        except Exception:
            logger.debug("[wechat_pc.uia] finish_voice_record 失败", exc_info=True)
            return False

    def cancel_voice_record(self) -> bool:
        """点「取消」放弃录音；返回是否已**确认**退出录音态（不在录音态即 True）。**绝不发 Esc**（会把主窗藏进托盘）。"""
        main = self._main_window()
        if main is None:
            return False
        try:
            if self._record_view(main) is None:
                return True
            self._click_in_record_view(main, "voice_cancel", fallback_named=False)
            return self._wait_recording(main, False)
        except Exception:
            logger.debug("[wechat_pc.uia] cancel_voice_record 失败", exc_info=True)
            return False

    # ── 资料卡 ──
    def _ensure_chat_info_panel(self, main: Any) -> Optional[Any]:
        """打开（切换式）「聊天信息」侧栏并返回头像按钮；已开则直接返回。最多点两次。"""
        heads = self._find(main, "contact_head")
        for _ in range(2):
            if heads:
                return heads[0]
            btn = self._find(main, "chat_info_button")
            if not btn:
                return None
            self._activate(main)
            try:
                btn[0].Click(simulateMove=True)
            except Exception:
                return None
            time.sleep(1.2)
            heads = self._find(main, "contact_head")
        return heads[0] if heads else None

    def _close_popups(self, before: set) -> None:
        for c in self._qt_windows():
            try:
                h = int(c.NativeWindowHandle)
            except Exception:
                continue
            if h in before:
                continue
            try:
                wp = c.GetWindowPattern()
                if wp:
                    wp.Close()
            except Exception:
                pass

    def read_profile_wxid(self, display_name: str = "") -> str:
        """当前打开会话的对方微信号（资料卡弹窗「微信号：」值）；读不到空串。侧栏与弹窗用完即关。"""
        main = self._main_window()
        if main is None:
            return ""
        before = set()
        for c in self._qt_windows():
            try:
                before.add(int(c.NativeWindowHandle))
            except Exception:
                pass
        head = self._ensure_chat_info_panel(main)
        if head is None:
            return ""
        wxid = ""
        try:
            head.Click(simulateMove=True)
            time.sleep(1.2)
            popup = None
            for c in self._qt_windows():
                if _s(getattr(c, "ClassName", "")) == PROFILE_POPUP_CLASS:
                    popup = c
                    break
            if popup is not None:
                wxid = self._wxid_from_profile(popup)
        except Exception:
            logger.debug("[wechat_pc.uia] 读资料卡失败", exc_info=True)
        finally:
            self._close_popups(before)
            # 侧栏再点一次关掉，还原布局（切换式）
            try:
                btn = self._find(main, "chat_info_button")
                if btn and self._find(main, "contact_head"):
                    btn[0].Click(simulateMove=True)
                    time.sleep(0.4)
            except Exception:
                pass
        return wxid

    def _self_avatar(self, main: Any) -> Optional[Any]:
        """导航栏本人头像按钮：必须在会话列表左侧（排除会话格内头像与右侧资料栏的对方头像），取最上方的一个。"""
        lists = self._find(main, "session_list")
        try:
            limit_x = int(lists[0].BoundingRectangle.left) if lists else int(main.BoundingRectangle.left) + 90
        except Exception:
            return None

        def _left_of_list(c: Any) -> Optional[Tuple[int, int]]:
            try:
                r = c.BoundingRectangle
                if r.right <= limit_x and r.right > r.left and r.bottom > r.top:
                    return (int(r.top), int(r.left))
            except Exception:
                pass
            return None

        best: List[Tuple[Tuple[int, int], Any]] = []
        for c in self._find(main, "self_avatar", limit=8):
            pos = _left_of_list(c)
            if pos is not None:
                best.append((pos, c))
        return min(best, key=lambda x: x[0])[1] if best else None

    def _self_avatar_point(self, main: Any) -> Optional[Tuple[int, int]]:
        """4.1.13 真机：头像不在 UIA 树里，但它就在左侧 ``mmui::MainTabBar`` 顶部、第一个 ``XTabBarItem``（「微信」）之上的空白带；
        点那一带的中心就弹本人资料卡。没有那带（不足 40px）就不猜。"""
        bar = None
        deadline = time.monotonic() + UIA_OP_TIMEOUT
        for c in _walk(main, 8):
            if time.monotonic() > deadline:
                return None
            if _s(getattr(c, "ClassName", "")) == "mmui::MainTabBar":
                bar = c
                break
        if bar is None:
            return None
        try:
            r = bar.BoundingRectangle
            first_tab = min((int(t.BoundingRectangle.top) for t in _walk(bar, 6)
                             if _s(getattr(t, "ClassName", "")) == "mmui::XTabBarItem"), default=0)
            if first_tab - int(r.top) < 40 or r.right <= r.left:
                return None
            return (int(r.left + r.right) // 2, (int(r.top) + first_tab) // 2)
        except Exception:
            return None

    def read_self_identity(self) -> Dict[str, str]:
        """登录微信的昵称/微信号（点导航栏头像 → 本人资料卡）；读不到的键为空串。弹窗用完即关。"""
        out = {"nick": "", "wxid": ""}
        main = self._main_window()
        if main is None:
            return out
        before = set()
        for c in self._qt_windows():
            try:
                before.add(int(c.NativeWindowHandle))
            except Exception:
                pass
        avatar = self._self_avatar(main)
        point = None if avatar is not None else self._self_avatar_point(main)
        if avatar is None and point is None:
            return out
        try:
            self._activate(main)
            if avatar is not None:
                avatar.Click(simulateMove=True)
            else:
                _uia.Click(point[0], point[1])
            time.sleep(1.2)
            popup = None
            for c in self._qt_windows():
                try:
                    h = int(c.NativeWindowHandle)
                except Exception:
                    continue
                if h not in before and (_s(getattr(c, "ClassName", "")) == PROFILE_POPUP_CLASS or popup is None):
                    popup = c
            if popup is not None:
                out["wxid"] = self._wxid_from_profile(popup)
                out["nick"] = self._nick_from_profile(popup, out["wxid"])
        except Exception:
            logger.debug("[wechat_pc.uia] 读本人资料卡失败", exc_info=True)
        finally:
            self._close_popups(before)
        return out

    def _nick_from_profile(self, popup: Any, wxid: str) -> str:
        """资料卡里的昵称：第一条既不是字段标签（「微信号：」…）也不是微信号本身的非空文本。"""
        labels = set(self.anchors["profile_wxid_key"]["name"])
        for c in _walk(popup, 24):
            if getattr(c, "ControlTypeName", "") != "TextControl":
                continue
            name = _s(getattr(c, "Name", "")).strip()
            if not name or name in labels or name.rstrip("：:").endswith(("微信号", "WeChat ID", "地区", "Region")):
                continue
            if wxid and parse_wxid(name) == wxid:
                continue
            return name[:64]
        return ""

    def _wxid_from_profile(self, popup: Any) -> str:
        nodes = [c for c in _walk(popup, 24) if getattr(c, "ControlTypeName", "") == "TextControl"]
        for i, c in enumerate(nodes):
            if _matches(c, self.anchors["profile_wxid_key"]):
                for nxt in nodes[i + 1:i + 3]:
                    v = parse_wxid(_s(getattr(nxt, "Name", "")))
                    if v:
                        return v
        # 兜底：任何 ProfileTextView 里形如微信号的值
        for c in nodes:
            if _matches(c, self.anchors["profile_value"]):
                v = parse_wxid(_s(getattr(c, "Name", "")))
                if v and not v.isdigit():
                    return v
        return ""


__all__ = ["ANCHORS", "REQUIRED_FOR_READ", "REQUIRED_FOR_SEND", "UiaBackend", "available",
           "parse_session_cell_name", "bubble_direction_by_pixels", "classify_bubble_direction",
           "background_reference", "parse_time_label", "compute_unread_flags", "bubble_kind_for", "backfill_ts_hints"]
