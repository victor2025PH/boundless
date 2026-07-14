"""
BaseAutomation — shared foundation for Telegram, LinkedIn, WhatsApp modules.

Provides:
- Unified device management (set_current_device, _did, _u2)
- Integrated HumanBehavior engine (self.hb)
- Integrated ComplianceGuard (self.guard)
- guarded() context manager: compliance check → action → record → delay
- Common navigation (go_back, go_home, ensure_main_screen)
- Screenshot/hierarchy dump helpers
- AI integration: VisionFallback, MessageRewriter, AutoReply
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional, Any, List, Dict

from ..device_control.device_manager import DeviceManager, get_device_manager
from ..behavior.human_behavior import HumanBehavior, BehaviorProfile, get_profile
from ..behavior.compliance_guard import ComplianceGuard, QuotaExceeded, get_compliance_guard

import subprocess
import random


class _AdbUiObject:
    """ADB-based UiObject that uses accessibility node info for element finding."""

    def __init__(self, device, text=None, textContains=None, description=None,
                 descriptionContains=None, resourceId=None, className=None,
                 packageName=None, clickable=None, **kwargs):
        self._dev = device
        self._sel = {k: v for k, v in {
            "text": text, "textContains": textContains,
            "description": description, "descriptionContains": descriptionContains,
            "resourceId": resourceId, "className": className,
            "packageName": packageName, "clickable": clickable,
        }.items() if v is not None}
        self._sel.update(kwargs)
        self._bounds = None
        self._center = None
        self._text_cache = None
        self._content_desc_cache = ""
        self._class_cache = ""
        self._clickable_cache = False
        self._found = None
        self._matched_xml: Any = None  # 真匹配中的 XMLElement

    def _try_xml_match(self) -> bool:
        """用真 dump_hierarchy XML 找元素,支持 text/desc/resourceId/className 多种 selector。

        Sprint 3 P3 加固: 之前 _AdbUiObject 仅有 TikTok 硬编码坐标 map,
        对 FB / WhatsApp / LinkedIn 等 app 全部失效。现在统一走 XML。
        """
        import logging as _lg
        _log = _lg.getLogger("src.app_automation._AdbUiObject")
        try:
            xml = self._dev.dump_hierarchy()
        except Exception as e:
            _log.debug("[xml_match] dump_hierarchy 失败: %s", e)
            return False
        if not xml or "<hierarchy" not in xml:
            _log.debug("[xml_match] dump_hierarchy 返回空 (sel=%s)", self._sel)
            return False
        try:
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception as e:
            _log.debug("[xml_match] XMLParser.parse 失败: %s", e)
            return False
        if not elements:
            _log.debug("[xml_match] XMLParser.parse 返回 0 元素")
            return False

        text = (self._sel.get("text") or "").strip()
        text_c = (self._sel.get("textContains") or "").strip()
        desc = (self._sel.get("description") or "").strip()
        desc_c = (self._sel.get("descriptionContains") or "").strip()
        rid = (self._sel.get("resourceId") or "").strip()
        cls = (self._sel.get("className") or "").strip()
        click_req = self._sel.get("clickable")

        text_l = text.lower(); text_c_l = text_c.lower()
        desc_l = desc.lower(); desc_c_l = desc_c.lower()
        cls_l = cls.lower(); rid_l = rid.lower()

        candidates = []  # (priority_score, area, element)

        for el in elements:
            if not el.enabled:
                continue
            el_text = (getattr(el, "text", "") or "").strip()
            el_desc = (getattr(el, "content_desc", "") or "").strip()
            el_rid = (getattr(el, "resource_id", "") or "").strip()
            el_cls = (getattr(el, "class_name", "") or "").strip()
            el_click = bool(getattr(el, "clickable", False))

            # 必须满足所有给定 selector 字段
            ok = True
            if text and el_text.lower() != text_l: ok = False
            if ok and text_c and text_c_l not in el_text.lower(): ok = False
            if ok and desc and el_desc.lower() != desc_l: ok = False
            if ok and desc_c and desc_c_l not in el_desc.lower(): ok = False
            if ok and rid and el_rid.lower() != rid_l:
                # 兼容 cache 里只写后缀的写法
                if not el_rid.lower().endswith(rid_l):
                    ok = False
            if ok and cls and el_cls.lower() != cls_l: ok = False
            if ok and click_req is True and not el_click: ok = False
            if ok and click_req is False and el_click: ok = False
            if not ok:
                continue

            # 优先级: 可点击 > 不可点击; 字段越多越精确
            prio = (1 if el_click else 0) * 100
            if rid: prio += 30
            if text or desc: prio += 20
            if cls: prio += 5
            l, t, r, b = el.bounds
            area = max(1, (r - l) * (b - t))
            candidates.append((prio, area, el))

        if not candidates:
            _log.info("[xml_match] sel=%s found 0 candidates in %d elements",
                      self._sel, len(elements))
            return False
        _log.info("[xml_match] sel=%s found %d candidates in %d elements",
                  self._sel, len(candidates), len(elements))
        # 优先级最高,再面积大且非全屏
        candidates.sort(key=lambda x: (-x[0], -x[1]))
        best = candidates[0][2]
        l, t, r, b = best.bounds
        cx, cy = (l + r) // 2, (t + b) // 2
        if cx <= 0 or cy <= 0:
            return False
        self._bounds = (cx, cy)
        self._center = (cx, cy)
        self._matched_xml = best
        self._text_cache = best.text
        self._content_desc_cache = best.content_desc
        self._class_cache = best.class_name
        self._clickable_cache = best.clickable
        return True

    def _find(self, timeout=2.0):
        """Try to find the element using:
          1) 真实 dump_hierarchy XML 匹配 (Sprint 3 P3 加强,通用!)
          2) TikTok 硬编码坐标映射 (仅对 TikTok 包生效;
             s4_5 真机回归发现:FB 'Search Facebook' 被 'search' 子串误
             命中 TikTok 硬编码搜索坐标 (633,96) ,tap 到 XSpace 按钮。
             加 package guard 阻断跨 app 污染。)
        """
        if self._found is not None:
            return self._found

        if self._try_xml_match():
            self._found = True
            return True

        try:
            cur_pkg = ""
            if hasattr(self._dev, "_app_cache"):
                cur_pkg = (self._dev._app_cache or {}).get("pkg", "") or ""
            if cur_pkg and "trill" not in cur_pkg and "musically" not in cur_pkg \
                    and "tiktok" not in cur_pkg:
                import logging as _lg
                _lg.getLogger("src.app_automation._AdbUiObject").debug(
                    "[_find] skip TikTok-hardcoded path (cur_pkg=%s sel=%s)",
                    cur_pkg, self._sel)
                self._found = False
                return False
        except Exception:
            pass

        # === 路径 2: TikTok 老硬编码 (仅对 TikTok 前台) ===
        w, h = self._dev._w, self._dev._h
        text = self._sel.get("text", "")
        textC = self._sel.get("textContains", "")
        desc = self._sel.get("description", "")
        descC = self._sel.get("descriptionContains", "")
        cls = self._sel.get("className", "")
        keyword = (text or textC or desc or descC).lower()

        # TikTok UI 元素坐标映射 (基于 720x1600 Redmi 13C 真机测量)
        # 分组：视频页 / 资料页 / DM页 / 导航栏 / 搜索 / 通知
        coord_map = {
            # ── 视频页（For You）上的元素 ──
            "follow": (int(w * 0.50), int(h * 0.36)),        # 资料页 Follow 按钮
            "following": (int(w * 0.50), int(h * 0.36)),      # 同位置（状态不同）

            # ── 资料页 ──
            "message": (int(w * 0.75), int(h * 0.36)),        # 资料页 Message 按钮
            "messages": (int(w * 0.75), int(h * 0.36)),
            "followers": (int(w * 0.40), int(h * 0.28)),      # 粉丝数

            # ── DM 聊天页 ──
            "send a message": (int(w * 0.45), int(h * 0.95)), # DM 输入框
            "send": (int(w * 0.92), int(h * 0.95)),           # DM 发送按钮

            # ── 底部导航栏 ──
            "for you": (int(w * 0.18), int(h * 0.088)),       # For You Tab（顶部）
            "home": (int(w * 0.10), int(h * 0.968)),          # Home Tab
            "inbox": (int(w * 0.77), int(h * 0.968)),         # Inbox Tab

            # ── 搜索 ──
            "search": (int(w * 0.88), int(h * 0.06)),         # 搜索图标
            "users": (int(w * 0.33), int(h * 0.12)),          # 搜索结果 Users Tab

            # ── 通知/活动页 ──
            "all activity": (int(w * 0.50), int(h * 0.12)),
            "activity": (int(w * 0.50), int(h * 0.12)),
            "new followers": (int(w * 0.25), int(h * 0.15)),

            # ── 视频页互动 ──
            "like": (int(w * 0.93), int(h * 0.42)),            # 视频页点赞（心形）按钮
            "comment": (int(w * 0.93), int(h * 0.53)),         # 视频页评论按钮

            # ── 弹窗/确认 ──
            "unfollow": (int(w * 0.50), int(h * 0.55)),       # 取关确认按钮
        }

        # resourceId 特殊映射（TikTok 特有的资源 ID）
        rid = self._sel.get("resourceId", "")
        if rid:
            rid_map = {
                ":id/hpm": (int(w * 0.93), int(h * 0.38)),   # 视频页 Follow 按钮（创作者旁边的+号）
                ":id/zkr": (int(w * 0.93), int(h * 0.30)),   # 视频页创作者头像
                ":id/title": (int(w * 0.15), int(h * 0.88)),  # 视频页创作者名字
                ":id/desc": (int(w * 0.30), int(h * 0.90)),   # 视频页描述文字
                ":id/qwm": (int(w * 0.35), int(h * 0.28)),   # 资料页统计数字
                ":id/qwl": (int(w * 0.35), int(h * 0.30)),   # 资料页统计标签
                ":id/mvd": (int(w * 0.10), int(h * 0.968)),   # Home Tab
                ":id/mve": (int(w * 0.77), int(h * 0.968)),   # Inbox Tab
                ":id/mvf": (int(w * 0.92), int(h * 0.968)),   # Profile Tab
            }
            for suffix, coords in rid_map.items():
                if rid.endswith(suffix):
                    self._bounds = coords
                    self._found = True
                    return True

        # descriptionContains 特殊处理：profile 在不同上下文含义不同
        if descC and "profile" in descC.lower():
            # 在视频页时是创作者头像（右侧），不是底部 Profile Tab
            self._bounds = (int(w * 0.93), int(h * 0.30))
            self._found = True
            return True

        # comment 按钮（视频页右侧）
        if descC and "comment" in descC.lower():
            self._bounds = (int(w * 0.93), int(h * 0.48))
            self._found = True
            return True

        # EditText 类匹配
        if cls and "edittext" in cls.lower():
            self._bounds = (int(w * 0.45), int(h * 0.95))  # DM 输入框
            self._found = True
            return True

        for key, coords in coord_map.items():
            if key in keyword:
                self._bounds = coords
                self._found = True
                return True

        # 未匹配：对 "followed you" 之类的动态文本，返回 False
        self._found = False
        return False

    def exists(self, timeout=2.0):
        return self._find(timeout)

    def click(self):
        if self._find():
            self._dev.click(*self._bounds)

    def get_text(self):
        return self._text_cache or ""

    def set_text(self, text):
        if not self._find():
            return
        self._dev.click(*self._bounds)
        import time
        time.sleep(0.3)
        # 清空已有文本
        self._dev._adb("shell input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67")
        time.sleep(0.1)

        # 2026-04-24 关键修复: unicode (中/日/韩) 文本走原生 u2.Device.set_text
        # 因为 `adb shell input text` 只支持 ASCII, 非 ASCII 会被吞或乱码.
        # 借 dm.get_u2 拿原生 Device, 用同样 selector 找 element 后 setText
        # (u2 的 setText 走 AccessibilityNodeInfo ACTION_SET_TEXT, 支持任意 unicode).
        is_ascii = all(ord(c) < 128 for c in text)
        if not is_ascii:
            try:
                dm = getattr(self._dev, "_dm", None)
                did = getattr(self._dev, "_did", None)
                if dm is not None and did:
                    u2_d = dm.get_u2(did)
                    if u2_d is not None:
                        # 用同一 selector 在原生 u2 找 element
                        u2_el = u2_d(**self._sel)
                        if u2_el.exists(timeout=1.5):
                            u2_el.set_text(text)
                            self._dev.invalidate_dump_cache()
                            return
            except Exception as e:
                import logging as _lg
                _lg.getLogger("src.app_automation._AdbUiObject").debug(
                    "[set_text] u2 原生路径失败, 回退 adb input text: %s", e)

        # ASCII 或 u2 兜底失败: adb input text
        safe = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self._dev._adb(f'shell input text "{safe}"')
        self._dev.invalidate_dump_cache()

    def clear_text(self):
        if self._find():
            self._dev.click(*self._bounds)
            import time
            time.sleep(0.2)
            self._dev._adb("shell input keyevent 28")  # KEYCODE_CLEAR
            self._dev._adb("shell input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67")

    @property
    def info(self):
        if self._matched_xml is not None:
            l, t, r, b = self._matched_xml.bounds
            return {
                "bounds": {"left": l, "top": t, "right": r, "bottom": b},
                "text": self._text_cache or "",
                "contentDescription": self._content_desc_cache or "",
                "className": self._class_cache or "",
                "clickable": self._clickable_cache,
                "enabled": True,
            }
        b = self._bounds or (0, 0)
        return {"bounds": {"left": b[0]-20, "top": b[1]-20,
                           "right": b[0]+20, "bottom": b[1]+20},
                "text": self._text_cache or "",
                "contentDescription": "",
                "className": "",
                "clickable": False,
                "enabled": True}

    @property
    def count(self):
        # 列表元素计数：返回一个合理的估计值
        return 3

    def __getitem__(self, idx):
        """支持索引访问（粉丝列表中的多个 Follow 按钮）"""
        w, h = self._dev._w, self._dev._h
        # 列表项间距约 70px，从 y=250 开始
        y = int(h * 0.18) + idx * int(h * 0.065)
        obj = _AdbUiObject(self._dev)
        obj._bounds = (int(w * 0.50), y)
        obj._found = True
        return obj

    def sibling(self, **kwargs):
        """返回 sibling 的 fake 实现"""
        return _AdbUiObject(self._dev, **kwargs)


class AdbFallbackDevice:
    """ADB-based device proxy when uiautomator2 is unavailable.

    完整实现 TikTok 自动化所需的所有功能：
    - 触控：swipe, click, press
    - App 控制：app_start, app_stop, app_current
    - UI 查找：__call__ 返回 _AdbUiObject（基于坐标映射）
    - 文本输入：通过 ADB input text
    - 导航：通过 deeplink URL
    - 截图：screencap 获取屏幕状态
    """

    def __init__(self, device_id: str, dm: DeviceManager):
        self._did = device_id
        self._dm = dm
        self._log = logging.getLogger("AdbFallback")
        self._app_cache = {"pkg": "", "ts": 0}  # app_current 缓存
        # Sprint 4 P0 优化: dump_hierarchy TTL 缓存。
        # 动机: _detect_risk_dialog 一次风控检测轮询 10 个关键词,
        # 每个 d(textContains=kw).exists() 触发一次 dump,10 次 dump×0.4s
        # = 4-8 秒纯 UI 层获取浪费。200-350ms TTL 是甜蜜点:
        #   * 人类/业务逻辑的真实 UI 变化频率 < 500ms 级
        #   * 可跨同一循环里的所有 selector 共享一次 dump
        #   * 一旦发生 tap/swipe/press/app_start 等"写操作",主动 invalidate
        # 预期:每任务 dump 从 ~20 次 → 3-5 次,节省 70-85% IO。
        self._dump_cache = {"xml": "", "ts": 0.0}
        self._dump_ttl_s = 0.30  # 300ms TTL
        ok, out = dm.execute_adb_command("shell wm size", device_id)
        self._w, self._h = 720, 1600
        if ok and "x" in out:
            try:
                parts = out.strip().split()[-1].split("x")
                self._w, self._h = int(parts[0]), int(parts[1])
            except Exception:
                pass

    def window_size(self):
        """与 uiautomator2.Device 对齐，供 scroll / 点击相对坐标使用。"""
        return self._w, self._h

    def _adb(self, cmd: str, timeout: int = 15) -> str:
        ok, out = self._dm.execute_adb_command(cmd, self._did)
        return out if ok else ""

    def swipe(self, x1, y1, x2, y2, duration=0.3):
        """与 u2 同名,duration 默认按"秒"理解(u2 接口语义)。

        防御: 上层若不慎传了 ms (如 300),自动检测并转回秒,避免出现
        300_000ms 这种被 MIUI 当作"长按底部上滑"误识别的灾难。
        """
        try:
            d = float(duration)
        except (TypeError, ValueError):
            d = 0.3
        if d > 30:  # 一定是误传 ms,转回秒
            d = d / 1000.0
        dur_ms = max(100, min(3000, int(d * 1000)))
        self._adb(f"shell input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {dur_ms}")
        self.invalidate_dump_cache()

    def click(self, x, y):
        self._adb(f"shell input tap {int(x)} {int(y)}")
        self.invalidate_dump_cache()

    def long_click(self, x, y, duration=1.0):
        dur_ms = int(duration * 1000)
        self._adb(f"shell input swipe {int(x)} {int(y)} {int(x)} {int(y)} {dur_ms}")
        self.invalidate_dump_cache()

    def press(self, key):
        key_map = {"back": 4, "home": 3, "enter": 66, "menu": 82,
                   "search": 84}
        code = key_map.get(key, key)
        self._adb(f"shell input keyevent {code}")
        self.invalidate_dump_cache()

    def send_action(self, code=None):
        """触发 IME action (search / go / send / done).

        u2.Device.send_action 走 FastInputIME APK 广播; AdbFallbackDevice 上
        FastInputIME 通常未装 (MIUI 拒装第三方 IME APK), 改用纯 ADB 兜底.

        真机证据 (task 35b2e957 2026-04-30):
          KEYCODE_ENTER (66) 在 FB Android 搜索 EditText 上被 typeahead overlay
          截获 → 选中首位建议 (人物 profile). 改用 KEYCODE_SEARCH (84) 走另一
          条路径, 通常映射到 imeOptions=actionSearch 触发 onEditorAction(SEARCH).
          若仍无效, 兜底 KEYCODE_ENTER 维持原行为 (再由上层 Step 2.5 拦下).

        Args:
            code: 'search' / 'go' / 'send' / 'done'. 当前实现仅区分 search vs 其他,
                  其他统一走 ENTER. 未来如需精细区分可扩展.
        """
        action = (str(code).lower() if code is not None else "search")
        if action == "search":
            # 优先 KEYCODE_SEARCH (84): 避开 FB typeahead 对 ENTER 的拦截
            self._adb("shell input keyevent 84")
        else:
            # go/send/done — 用 ENTER 兜底
            self._adb("shell input keyevent 66")
        self.invalidate_dump_cache()

    def app_start(self, package):
        self._adb(f"shell monkey -p {package} -c android.intent.category.LAUNCHER 1")
        self.invalidate_dump_cache()
        self.invalidate_app_cache()

    def app_stop(self, package):
        self._adb(f"shell am force-stop {package}")
        self.invalidate_dump_cache()
        self.invalidate_app_cache()

    def app_current(self):
        import time as _t
        # 缓存: 10秒内复用上次结果（dumpsys 很慢，每次2-3秒）
        now = _t.time()
        if now - self._app_cache["ts"] < 10 and self._app_cache["pkg"]:
            return {"package": self._app_cache["pkg"]}

        out = self._adb("shell dumpsys activity activities")
        for keyword in ["topResumedActivity", "mResumedActivity", "mFocusedApp"]:
            for line in out.splitlines():
                if keyword in line:
                    for part in line.split():
                        if "/" in part and "." in part:
                            pkg = part.split("/")[0].lstrip("{")
                            self._app_cache = {"pkg": pkg, "ts": now}
                            return {"package": pkg}
        return {"package": ""}

    def invalidate_app_cache(self):
        """手动清除缓存（切换 APP 后调用）"""
        self._app_cache = {"pkg": "", "ts": 0}

    def app_info(self, package):
        out = self._adb(f"shell pm list packages {package}")
        return {"packageName": package} if package in out else None

    def freeze_rotation(self):
        self._adb("shell settings put system accelerometer_rotation 0")

    def set_orientation(self, orient="natural"):
        self._adb("shell settings put system user_rotation 0")

    @property
    def info(self):
        return {"displayWidth": self._w, "displayHeight": self._h}

    def __call__(self, **kwargs):
        """Selector call (e.g., d(text="For You")) — returns smart ADB-based UiObject."""
        return _AdbUiObject(self, **kwargs)

    def send_keys(self, text, clear=False):
        """通过 ADB 输入文本(Sprint 4 P0 加固版)。

        Sprint 4 P0 修复 P3.8 复盘遗留:
          ① 之前 clear 用 keyevent 28 (KEYCODE_CLEAR),MIUI/HyperOS 很多 IME
             不响应这个键,导致残留文本,叠加新输入变成乱码。
             改用 "Ctrl+A (keycode 29) + Delete (67)" 组合,覆盖所有 IME。
          ② 输入后 invalidate dump cache,让下一次 smart_find 读到最新 UI
          ③ 对包含特殊字符的文本,分段发送,避免单条过长被截断
        """
        import time
        if clear:
            # 全选 + 删除,对 MIUI GBoard/搜狗/MIUI 输入法都有效
            self._adb("shell input keyevent 29")  # KEYCODE_A (with meta = select all)
            time.sleep(0.05)
            self._adb("shell input keyevent 67")  # KEYCODE_DEL
            time.sleep(0.1)

        # 2026-04-24 关键修复: unicode 文本借原生 u2.Device.send_keys (走 IME/AdbKeyboard),
        # `adb shell input text` 只支持 ASCII. 中日文字符会被吞掉.
        is_ascii = all(ord(c) < 128 for c in text)
        if not is_ascii:
            try:
                u2_d = self._dm.get_u2(self._did) if self._dm else None
                if u2_d is not None:
                    u2_d.send_keys(text, clear=False)  # clear 已处理过
                    self.invalidate_dump_cache()
                    return
            except Exception as e:
                self._log.debug("[send_keys] u2 原生路径失败, 回退 adb input text: %s", e)

        # ADB input text:空格 %s,转义 \ " '
        safe = text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
        safe_adb = safe.replace(" ", "%s")
        # 分段:单段超过 80 字符容易被 IME 丢字,拆成 <=60 char/段
        if len(safe_adb) <= 60:
            self._adb(f'shell input text "{safe_adb}"')
        else:
            # 尽量在空格(%s) 边界切
            pieces = []
            buf = ""
            for token in safe_adb.split("%s"):
                if buf and len(buf) + len(token) + 2 > 60:
                    pieces.append(buf)
                    buf = token
                else:
                    buf = (buf + "%s" + token) if buf else token
            if buf:
                pieces.append(buf)
            for p in pieces:
                if p:
                    self._adb(f'shell input text "{p}"')
                    time.sleep(0.08)
        self.invalidate_dump_cache()

    def clear_text(self):
        """清空当前输入框。"""
        # 全选+删除
        self._adb("shell input keyevent 29 67")  # Ctrl+A, DEL
        import time
        time.sleep(0.1)

    def dump_hierarchy(self, force_refresh: bool = False):
        """返回真实 UI 层级,带超时保护 + 多路退路 + TTL 缓存(Sprint 4 P0)。

        历史:
          - Sprint 3 P2: u2 server 内阻塞死锁 → 加 5s 硬超时 + uiautomator dump 退路
          - Sprint 4 P0: 风控循环每轮 10 次 dump 太浪费 → 加 300ms TTL

        force_refresh=True 时绕过缓存,用于:
          - tap/swipe 等"写操作"后立即再读
          - 明知 UI 刚刚变化(如 app_start 后)
        """
        import time as _t
        now = _t.time()
        # 缓存命中:300ms 内直接返回上一次 dump
        if (not force_refresh
                and self._dump_cache["xml"]
                and (now - self._dump_cache["ts"]) < self._dump_ttl_s):
            return self._dump_cache["xml"]

        import concurrent.futures
        xml = ""
        # 路 1:借用 u2.dump_hierarchy,5 秒硬超时
        try:
            d = self._dm.get_u2(self._did) if self._dm else None
            if d is not None:
                def _u2_dump():
                    try:
                        return d.dump_hierarchy()
                    except Exception:
                        return ""
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_u2_dump)
                    xml = fut.result(timeout=5.0)
                if xml and "<hierarchy" in xml:
                    self._dump_cache = {"xml": xml, "ts": now}
                    return xml
        except concurrent.futures.TimeoutError:
            self._log.warning("[AdbFallback] u2.dump_hierarchy 5s 超时,降级 uiautomator")
        except Exception as e:
            self._log.warning("[AdbFallback] u2.dump_hierarchy 异常 %s: %s",
                              type(e).__name__, e)
        # 路 2:adb shell uiautomator dump → 落盘 cat
        path = "/sdcard/openclaw_ui.xml"
        self._adb(f"shell uiautomator dump {path}", timeout=8)
        out = self._adb(f"shell cat {path}", timeout=5)
        if out and "<hierarchy" in out:
            try:
                start = out.index("<hierarchy")
                end = out.rindex("</hierarchy>") + len("</hierarchy>")
                xml = out[start:end]
                self._dump_cache = {"xml": xml, "ts": now}
                return xml
            except ValueError:
                pass

    def invalidate_dump_cache(self):
        """写操作(tap/swipe/press/app_start)后调用,强制下次 dump 真读。"""
        self._dump_cache = {"xml": "", "ts": 0.0}
        return '<hierarchy rotation="0"></hierarchy>'

    def open_url(self, url):
        """通过 ADB 打开 URL（用于 deeplink 导航到用户资料）。"""
        self._adb(f'shell am start -a android.intent.action.VIEW -d "{url}"')


class BaseAutomation:
    """
    Abstract base for platform automation modules.

    Subclasses must set:
        PLATFORM: str          — "telegram" / "linkedin" / "whatsapp"
        PACKAGE: str           — Android package name
        MAIN_ACTIVITY: str     — main activity to launch (optional)
    """

    PLATFORM: str = ""
    PACKAGE: str = ""
    MAIN_ACTIVITY: str = ""

    def __init__(self, device_manager: DeviceManager,
                 behavior: Optional[HumanBehavior] = None,
                 guard: Optional[ComplianceGuard] = None):
        self.dm = device_manager
        self.logger = logging.getLogger(f"{__name__}.{self.PLATFORM}")
        self._current_device: Optional[str] = None
        self._current_account: str = ""

        self.hb = behavior or HumanBehavior(profile=get_profile(self.PLATFORM))
        self.guard = guard or get_compliance_guard()

        # AI modules — lazy-loaded to avoid import cost when AI is disabled
        self._vision = None
        self._rewriter = None
        self._auto_reply = None
        self._auto_selector = None

    # ── Device management ─────────────────────────────────────────────────

    def set_current_device(self, device_id: str) -> None:
        info = self.dm.get_device_info(device_id)
        if not info:
            raise ValueError(f"Device not found: {device_id}")
        self._current_device = device_id
        self.logger.info("Device set: %s (u2=%s)",
                         info.display_name,
                         "ok" if self.dm.get_u2(device_id) else "no")

    def _did(self, device_id: Optional[str] = None) -> str:
        did = device_id or self._current_device
        if not did:
            raise ValueError("No device_id specified")
        return did

    def _adb(self, cmd: str, device_id: Optional[str] = None,
             timeout: int = 15) -> str:
        """Execute an ADB command on a target device,returning combined stdout.

        真机修复 (P3): 之前 facebook.py 直接 self._adb(...) 但 BaseAutomation
        从未实现该方法,导致所有 fallback / xspace 兜底路径一旦触发就
        AttributeError。补上统一接口。
        """
        did = self._did(device_id)
        try:
            ok, out = self.dm.execute_adb_command(cmd, did, timeout=timeout)
        except TypeError:
            ok, out = self.dm.execute_adb_command(cmd, did)
        if not ok and out:
            self.logger.debug("[adb] %s => %s", cmd, out)
        return out if ok else (out or "")

    _force_adb_fallback = True  # MIUI 设备 u2 不稳定，强制用 ADB 模式

    def _u2(self, device_id: Optional[str] = None):
        """Get u2 device connection. Falls back to ADB-based device if u2 unavailable."""
        did = self._did(device_id)
        if self._force_adb_fallback:
            return AdbFallbackDevice(did, self.dm)
        d = self.dm.get_u2(did)
        if d:
            # 快速验证 u2 是否真的可用（3 秒超时）
            try:
                import concurrent.futures
                def _quick_test():
                    return d.info.get("displayWidth", 0) > 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_quick_test)
                    if future.result(timeout=3):
                        return d
            except Exception:
                self.logger.warning("[u2] 不稳定, ADB fallback: %s", did[:12])
        return AdbFallbackDevice(did, self.dm)

    def _u2_optional(self, device_id: Optional[str] = None):
        """Get u2 connection or None (for dual-path modules like Telegram)."""
        return self.dm.get_u2(self._did(device_id))

    # ── Compliance-aware action wrapper ───────────────────────────────────

    @contextmanager
    def guarded(self, action: str, account: Optional[str] = None,
                device_id: Optional[str] = None, weight: float = 1.0):
        """
        Context manager that wraps an action with compliance + behavior:

            with self.guarded("send_message"):
                self._do_send(d, msg)

        1. Checks quota (raises QuotaExceeded if exceeded)
        2. Yields (caller performs the action)
        3. Records the action
        4. Applies human-like post-action delay
        """
        acct = account or self._current_account
        did = self._did(device_id)
        _relaxed = False
        try:
            from src.host.fb_playbook import local_rules_disabled
            _relaxed = self.PLATFORM == "facebook" and local_rules_disabled()
        except Exception:
            _relaxed = False
        if not _relaxed:
            self.guard.check(self.PLATFORM, action, acct)
        try:
            yield
        finally:
            if not _relaxed:
                self.guard.record(self.PLATFORM, action, acct, did)
                self.hb.wait_between_actions(context_weight=weight)

    def check_quota(self, action: str, account: Optional[str] = None) -> bool:
        """Check if action is allowed without recording it."""
        try:
            self.guard.check(self.PLATFORM, action, account or self._current_account)
            return True
        except QuotaExceeded:
            return False

    def quota_status(self, account: Optional[str] = None) -> dict:
        return self.guard.get_platform_status(self.PLATFORM,
                                              account or self._current_account)

    # ── App lifecycle ─────────────────────────────────────────────────────

    def start_app(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        d = self._u2_optional(did)
        if d:
            d.app_start(self.PACKAGE)
            time.sleep(2.5)
            return d.app_current().get("package") == self.PACKAGE

        if self.MAIN_ACTIVITY:
            self.dm.execute_adb_command(
                f"shell am start -n {self.PACKAGE}/{self.MAIN_ACTIVITY}", did)
        else:
            self.dm.execute_adb_command(
                f"shell monkey -p {self.PACKAGE} -c android.intent.category.LAUNCHER 1", did)
        time.sleep(3)
        return True

    def stop_app(self, device_id: Optional[str] = None):
        did = self._did(device_id)
        d = self._u2_optional(did)
        if d:
            d.app_stop(self.PACKAGE)
        else:
            self.dm.execute_adb_command(f"shell am force-stop {self.PACKAGE}", did)
        time.sleep(1)

    def restart_app(self, device_id: Optional[str] = None) -> bool:
        self.stop_app(device_id)
        return self.start_app(device_id)

    def is_foreground(self, device_id: Optional[str] = None) -> bool:
        d = self._u2_optional(device_id)
        if d:
            try:
                return d.app_current().get("package") == self.PACKAGE
            except Exception:
                return False
        return False

    # ── Navigation ────────────────────────────────────────────────────────

    def go_back(self, device_id: Optional[str] = None):
        did = self._did(device_id)
        d = self._u2_optional(did)
        if d:
            d.press("back")
        else:
            self.dm.input_keyevent(did, "KEYCODE_BACK")
        time.sleep(0.5)

    def go_home(self, device_id: Optional[str] = None):
        did = self._did(device_id)
        d = self._u2_optional(did)
        if d:
            d.press("home")
        else:
            self.dm.input_keyevent(did, "KEYCODE_HOME")
        time.sleep(0.5)

    def ensure_main_screen(self, device_id: Optional[str] = None, max_backs: int = 5):
        """Press back until we reach the app's main screen or home."""
        did = self._did(device_id)
        for _ in range(max_backs):
            if not self.is_foreground(did):
                self.start_app(did)
                break
            self.go_back(did)
            time.sleep(0.3)

    # ── Helpers ───────────────────────────────────────────────────────────

    def screenshot(self, device_id: Optional[str] = None,
                   save_path: Optional[str] = None) -> Optional[str]:
        did = self._did(device_id)
        return self.dm.capture_screen(did, save_path)

    def dump_hierarchy(self, device_id: Optional[str] = None) -> str:
        d = self._u2(device_id)
        return d.dump_hierarchy()

    def find_and_click(self, d, selectors, timeout: float = 5) -> bool:
        """Try multiple selectors; click first match."""
        if isinstance(selectors, dict):
            selectors = [selectors]
        for sel in selectors:
            el = d(**sel)
            if el.exists(timeout=timeout):
                info = el.info
                cx = info["bounds"]["left"] + info["bounds"]["right"]
                cy = info["bounds"]["top"] + info["bounds"]["bottom"]
                self.hb.tap(d, cx // 2, cy // 2)
                return True
        return False

    def find_element(self, d, selectors, timeout: float = 5):
        """Try multiple selectors; return first existing element or None."""
        if isinstance(selectors, dict):
            selectors = [selectors]
        for sel in selectors:
            el = d(**sel)
            if el.exists(timeout=timeout):
                return el
        return None

    # ── AI Integration ─────────────────────────────────────────────────────

    @property
    def vision(self):
        """Lazy-load VisionFallback."""
        if self._vision is None:
            try:
                from ..ai.vision_fallback import VisionFallback
                self._vision = VisionFallback()
            except Exception as e:
                self.logger.debug("VisionFallback unavailable: %s", e)
        return self._vision

    @property
    def auto_selector(self):
        """Lazy-load AutoSelector (self-learning selector cache)."""
        if self._auto_selector is None:
            try:
                from ..vision.auto_selector import get_auto_selector
                self._auto_selector = get_auto_selector()
            except Exception as e:
                self.logger.debug("AutoSelector unavailable: %s", e)
        return self._auto_selector

    @property
    def rewriter(self):
        """Lazy-load MessageRewriter."""
        if self._rewriter is None:
            try:
                from ..ai.message_rewriter import get_rewriter
                self._rewriter = get_rewriter()
            except Exception as e:
                self.logger.debug("MessageRewriter unavailable: %s", e)
        return self._rewriter

    @property
    def auto_reply_engine(self):
        """Lazy-load AutoReply."""
        if self._auto_reply is None:
            try:
                from ..ai.auto_reply import AutoReply
                self._auto_reply = AutoReply()
            except Exception as e:
                self.logger.debug("AutoReply unavailable: %s", e)
        return self._auto_reply

    def find_and_click_with_vision(self, d, selectors, target_desc: str,
                                   context: str = "", timeout: float = 5) -> bool:
        """
        Enhanced find_and_click: selectors → AutoSelector cache → Vision fallback.

        Three-tier strategy:
          1. Try hardcoded selectors (fast, deterministic)
          2. Try AutoSelector learned cache (fast, no API cost)
          3. Use Vision to find element → learn new selector for next time
        """
        if self.find_and_click(d, selectors, timeout):
            return True

        # tier 2: AutoSelector (learned selectors from previous Vision calls)
        if self.auto_selector:
            parsed = self.auto_selector.find(d, self.PACKAGE, target_desc, context)
            if parsed and parsed.center != (0, 0):
                self.hb.tap(d, parsed.center[0], parsed.center[1])
                return True

        # tier 3: raw VisionFallback (legacy path, no learning)
        if self.vision and self.vision.budget_remaining > 0:
            self.logger.info("All selectors failed, trying raw Vision for '%s'", target_desc)
            result = self.vision.find_element(d, target_desc, context)
            if result and result.coordinates:
                self.hb.tap(d, result.coordinates[0], result.coordinates[1])
                return True

        return False

    def smart_find(self, target_desc: str, context: str = "",
                   device_id: Optional[str] = None):
        """
        Find any element by description using the AutoSelector engine.
        Returns ParsedElement or None.
        """
        if not self.auto_selector:
            return None
        d = self._u2(device_id)
        return self.auto_selector.find(d, self.PACKAGE, target_desc, context)

    def smart_tap(self, target_desc: str, context: str = "",
                  device_id: Optional[str] = None) -> bool:
        """Find and tap an element using smart self-learning selector."""
        parsed = self.smart_find(target_desc, context, device_id)
        if not parsed or parsed.center == (0, 0):
            self.logger.info("[smart_tap] MISS: '%s' (no match,center=%s)",
                             target_desc,
                             None if not parsed else parsed.center)
            return False
        d = self._u2(device_id)
        self.logger.info("[smart_tap] HIT '%s' @ %s",
                         target_desc, parsed.center)
        self.hb.tap(d, parsed.center[0], parsed.center[1])
        return True

    def rewrite_message(self, message: str,
                        context: Optional[Dict[str, str]] = None) -> str:
        """
        Rewrite a message for uniqueness using MessageRewriter.
        Falls back to original message if rewriter unavailable.
        """
        if self.rewriter:
            try:
                return self.rewriter.rewrite(message, context, self.PLATFORM)
            except Exception as e:
                self.logger.warning("Rewrite failed, using original: %s", e)
        return message

    def process_incoming_message(self, message: str, sender: str = "",
                                 conversation_id: str = "",
                                 persona: str = "casual") -> Optional[dict]:
        """
        Process an incoming message with AutoReply.
        Returns {"text": str, "delay_sec": float} or None if no reply needed.
        """
        engine = self.auto_reply_engine
        if not engine:
            return None
        try:
            result = engine.generate_reply(
                message=message,
                sender=sender,
                platform=self.PLATFORM,
                persona=persona,
                conversation_id=conversation_id,
            )
            if result:
                return {"text": result.text, "delay_sec": result.delay_sec,
                        "intent": result.intent}
        except Exception as e:
            self.logger.warning("AutoReply error: %s", e)
        return None
