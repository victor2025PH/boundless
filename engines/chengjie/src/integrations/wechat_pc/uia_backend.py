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
}
REQUIRED_FOR_READ = ("main_window", "session_list")
#: 发送必需：会话列表 + 消息列表 + 输入框 + 标题（消息列表/输入框/标题只有打开会话后才存在——
#: self_check 会先点开列表第一项再判）
REQUIRED_FOR_SEND = ("main_window", "session_list", "message_list", "composer", "chat_title", "send_button")

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

    def __init__(self, anchors: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
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
        self._last_check = 0.0

    # ── 窗口发现（Win32 → UIA） ──
    def _qt_windows(self) -> List[Any]:
        if not available():
            return []
        try:
            from src.integrations.wechat_pc.win32_windows import find_wechat_windows
        except Exception:
            return []
        out: List[Any] = []
        for w in find_wechat_windows(visible_only=True):
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
            btns = self._find(main, "send_button")
            if btns:
                btns[0].Click(simulateMove=True)
                return True
            eds = self._find(main, "composer")
            if eds:
                eds[0].SendKeys("{Enter}")
                return True
        except Exception:
            logger.debug("[wechat_pc.uia] press_send 失败", exc_info=True)
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
