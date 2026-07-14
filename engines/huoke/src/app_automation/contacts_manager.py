# -*- coding: utf-8 -*-
"""
通讯录管理器 — ADB 批量注入 + 读取 + TikTok 通讯录好友发现自动化。

功能:
  1. 批量注入号码到手机通讯录 (ADB content provider)
  2. 读取手机现有通讯录
  3. 清理注入的号码（保留原有联系人）
  4. TikTok "通讯录好友" 页面自动化（关注 + 发消息）
  5. 与 ChatBrain 集成（source=contact 策略）

使用:
  mgr = ContactsManager(adb_serial='DEVICE_SERIAL')
  mgr.inject_contacts([{'name':'Marco','number':'+39xxx'}])
  found = mgr.tiktok_find_contact_friends(d)  # uiautomator2 device
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.host.device_registry import data_file
from src.utils.subprocess_text import run as _sp_run_text

log = logging.getLogger(__name__)

ADB_EXE = r"C:\platform-tools\adb.exe"
INJECT_PREFIX = "OC_"

# 国家电话前缀映射，用于号码标准化
_COUNTRY_PREFIXES = {
    "IT": "+39", "US": "+1", "GB": "+44", "DE": "+49", "FR": "+33",
    "ES": "+34", "BR": "+55", "IN": "+91", "RU": "+7", "JP": "+81",
    "KR": "+82", "AU": "+61", "CA": "+1", "MX": "+52", "AR": "+54",
    "NL": "+31", "BE": "+32", "CH": "+41", "AT": "+43", "PT": "+351",
    "PL": "+48", "SE": "+46", "NO": "+47", "DK": "+45", "FI": "+358",
    "TR": "+90", "SA": "+966", "AE": "+971", "EG": "+20", "ZA": "+27",
    "NG": "+234", "TH": "+66", "VN": "+84", "PH": "+63", "ID": "+62",
    "MY": "+60", "SG": "+65", "TW": "+886", "HK": "+852",
}


def normalize_phone(number: str, country_code: str = "") -> str:
    """标准化电话号码为 E.164 格式"""
    n = re.sub(r'[\s\-\(\)\.]+', '', number.strip())
    if n.startswith('+'):
        return n
    if n.startswith('00'):
        return '+' + n[2:]
    prefix = _COUNTRY_PREFIXES.get(country_code.upper(), "")
    if prefix and n.startswith('0'):
        return prefix + n[1:]
    if prefix:
        return prefix + n
    return n


@dataclass
class ContactEntry:
    name: str
    number: str
    tags: List[str] = field(default_factory=list)
    source: str = ""
    injected: bool = False


class ContactsManager:
    """设备通讯录管理（单设备实例）"""

    def __init__(self, adb_serial: str):
        self.serial = adb_serial

    def _adb(self, *args, timeout: int = 15) -> str:
        cmd = [ADB_EXE, "-s", self.serial] + list(args)
        r = _sp_run_text(cmd, capture_output=True, timeout=timeout)
        return r.stdout + r.stderr

    # ─── 读取通讯录 ───
    def list_contacts(self) -> List[Dict[str, str]]:
        """读取设备通讯录"""
        out = self._adb(
            "shell", "content", "query",
            "--uri", "content://contacts/phones/",
            "--projection", "display_name:number",
        )
        contacts = []
        for line in out.splitlines():
            name_m = re.search(r'display_name=([^,]+)', line)
            num_m = re.search(r'number=([^,\s]+)', line)
            if name_m:
                contacts.append({
                    "name": name_m.group(1).strip(),
                    "number": num_m.group(1).strip() if num_m else "",
                })
        return contacts

    # ─── 单条注入 ───
    def add_contact(self, name: str, number: str) -> bool:
        """向设备添加一个联系人"""
        # 添加前缀标记，方便后续清理
        tagged_name = name if name.startswith(INJECT_PREFIX) else INJECT_PREFIX + name
        try:
            # 创建 raw_contact
            r0 = self._adb(
                "shell", "content", "insert",
                "--uri", "content://com.android.contacts/raw_contacts",
                "--bind", "account_type:s:",
                "--bind", "account_name:s:",
            )
            rid_m = re.search(r'(\d+)', r0)
            rid = rid_m.group(1) if rid_m else "1"

            # 写入姓名
            self._adb(
                "shell", "content", "insert",
                "--uri", "content://com.android.contacts/data",
                "--bind", f"raw_contact_id:i:{rid}",
                "--bind", "mimetype:s:vnd.android.cursor.item/name",
                "--bind", f"data1:s:{tagged_name}",
            )
            # 写入号码
            self._adb(
                "shell", "content", "insert",
                "--uri", "content://com.android.contacts/data",
                "--bind", f"raw_contact_id:i:{rid}",
                "--bind", "mimetype:s:vnd.android.cursor.item/phone_v2",
                "--bind", f"data1:s:{number}",
                "--bind", "data2:i:1",
            )
            return True
        except Exception as e:
            log.warning(f"添加联系人失败 {name}: {e}")
            return False

    # ─── 批量注入 ───
    def inject_contacts(
        self, contacts: List[Dict[str, str]],
        delay: float = 0.3,
        country_code: str = "",
    ) -> Dict[str, int]:
        """
        批量注入联系人到设备（自动标准化号码、去重）。

        Args:
            contacts: [{"name": "Marco", "number": "+39xxx"}, ...]
            delay: 每条之间的延迟（避免 ADB 阻塞）
            country_code: 目标国家代码(IT/US...)，用于号码标准化

        Returns:
            {"total": N, "success": M, "failed": K, "skipped": S, "details": [...]}
        """
        existing = {c["number"] for c in self.list_contacts() if c.get("number")}
        result = {
            "total": len(contacts), "success": 0, "failed": 0,
            "skipped": 0, "details": [],
        }

        for c in contacts:
            name = c.get("name", "")
            number = normalize_phone(c.get("number", ""), country_code)
            if not name or not number:
                result["skipped"] += 1
                result["details"].append({"name": name, "status": "invalid"})
                continue
            if number in existing:
                result["skipped"] += 1
                result["details"].append({"name": name, "status": "duplicate"})
                continue

            ok = self.add_contact(name, number)
            if ok:
                result["success"] += 1
                existing.add(number)
                result["details"].append({"name": name, "status": "ok"})
            else:
                result["failed"] += 1
                result["details"].append({"name": name, "status": "error"})

            if delay > 0:
                time.sleep(delay)

        log.info(f"[通讯录] 批量注入完成: total={result['total']} success={result['success']}")
        return result

    # ─── 导出通讯录 ───
    def export_csv(self, output_path: str, only_injected: bool = False) -> int:
        """导出设备通讯录到 CSV（支持跨设备同步）"""
        contacts = self.list_contacts()
        if only_injected:
            contacts = [c for c in contacts if c["name"].startswith(INJECT_PREFIX)]
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("name,number\n")
            for c in contacts:
                name = c["name"].replace(INJECT_PREFIX, "") if c["name"].startswith(INJECT_PREFIX) else c["name"]
                f.write(f"{name},{c.get('number','')}\n")
        log.info(f"[通讯录] 已导出 {len(contacts)} 条到 {output_path}")
        return len(contacts)

    # ─── 清理已注入的联系人 ───
    def clean_injected(self) -> int:
        """删除所有 OC_ 前缀的联系人（保留原有通讯录）"""
        contacts = self.list_contacts()
        removed = 0
        for c in contacts:
            if c["name"].startswith(INJECT_PREFIX):
                try:
                    self._adb(
                        "shell", "content", "delete",
                        "--uri", "content://com.android.contacts/raw_contacts",
                        "--where", f"display_name='{c['name']}'",
                    )
                    removed += 1
                except Exception:
                    pass
        log.info(f"[通讯录] 已清理 {removed} 条注入联系人")
        return removed

    # ─── CSV 导入 ───
    @staticmethod
    def parse_csv(csv_path: str, country_code: str = "") -> List[Dict[str, str]]:
        """解析 CSV/TXT 为联系人列表（智能检测列顺序、支持多种分隔符）"""
        import csv
        contacts = []
        path = Path(csv_path)
        if not path.exists():
            return []

        raw = path.read_text(encoding="utf-8-sig")
        # 检测分隔符
        sep = ","
        first_line = raw.split("\n", 1)[0]
        if "\t" in first_line and "," not in first_line:
            sep = "\t"
        elif ";" in first_line and "," not in first_line:
            sep = ";"

        lines = raw.strip().split("\n")
        if not lines:
            return []

        header = [h.lower().strip().strip('"') for h in lines[0].split(sep)]
        # 智能列匹配
        name_aliases = {"name", "nome", "姓名", "名前", "nombre", "display_name", "full_name"}
        num_aliases = {"number", "phone", "tel", "telephone", "号码", "电话", "telefono", "mobile", "cell"}
        name_idx = next((i for i, h in enumerate(header) if h in name_aliases), 0)
        num_idx = next((i for i, h in enumerate(header) if h in num_aliases), 1 if name_idx == 0 else 0)

        for line in lines[1:]:
            cols = [c.strip().strip('"') for c in line.split(sep)]
            if len(cols) > max(name_idx, num_idx):
                name = cols[name_idx]
                number = normalize_phone(cols[num_idx], country_code)
                if name and number:
                    contacts.append({"name": name, "number": number})
        return contacts


# ─── 联系人匹配状态持久化 ───

_DISCOVERY_DB_PATH = str(data_file("contact_discovery.db"))


def _ensure_discovery_db():
    """确保联系人发现数据库和表存在"""
    import sqlite3
    Path(_DISCOVERY_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DISCOVERY_DB_PATH, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            contact_name TEXT NOT NULL,
            tiktok_username TEXT DEFAULT '',
            matched_at TEXT DEFAULT (datetime('now')),
            followed INTEGER DEFAULT 0,
            messaged INTEGER DEFAULT 0,
            message_text TEXT DEFAULT '',
            platform TEXT DEFAULT 'tiktok',
            UNIQUE(device_id, contact_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_device ON discovered_friends(device_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_name ON discovered_friends(contact_name)")
    conn.commit()
    conn.close()


def save_discovery_result(device_id: str, contact_name: str,
                          tiktok_username: str = "",
                          followed: bool = False, messaged: bool = False,
                          message_text: str = ""):
    """保存单条好友发现结果到数据库"""
    import sqlite3
    _ensure_discovery_db()
    try:
        conn = sqlite3.connect(_DISCOVERY_DB_PATH, timeout=5)
        conn.execute(
            "INSERT OR REPLACE INTO discovered_friends "
            "(device_id, contact_name, tiktok_username, followed, messaged, message_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, contact_name, tiktok_username,
             1 if followed else 0, 1 if messaged else 0, message_text),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[通讯录发现] 保存结果失败: {e}")


def get_discovery_results(device_id: str = "") -> List[Dict]:
    """读取好友发现结果（可按设备过滤）"""
    import sqlite3
    _ensure_discovery_db()
    try:
        conn = sqlite3.connect(_DISCOVERY_DB_PATH, timeout=5)
        if device_id:
            rows = conn.execute(
                "SELECT contact_name, tiktok_username, followed, messaged, matched_at, platform "
                "FROM discovered_friends WHERE device_id=?", (device_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT contact_name, tiktok_username, followed, messaged, matched_at, platform "
                "FROM discovered_friends"
            ).fetchall()
        conn.close()
        return [{"contact_name": r[0], "tiktok_username": r[1],
                 "followed": bool(r[2]), "messaged": bool(r[3]),
                 "matched_at": r[4], "platform": r[5]} for r in rows]
    except Exception:
        return []


# ─── TikTok 通讯录好友发现（UI 自动化）───

def tiktok_find_contact_friends(
    tiktok_automation,  # TikTokAutomation 实例
    device_id: str,
    max_friends: int = 20,
    auto_follow: bool = True,
    auto_message: bool = True,
    target_language: str = "",
    contact_info: str = "",
) -> Dict[str, Any]:
    """
    在 TikTok 中查找通讯录好友并执行操作。

    流程: 我(Profile) → 添加好友 → 通讯录(Contacts) → 遍历好友 → 关注 + 发消息
    发现的好友自动写入 contact_discovery.db 供 enriched API 使用。
    """
    d = tiktok_automation._u2(tiktok_automation._did(device_id))
    hb = tiktok_automation.hb
    results = {"found": 0, "followed": 0, "messaged": 0, "errors": [], "discovered_names": []}

    try:
        # 1. 进入个人页
        tiktok_automation.go_profile(d)
        time.sleep(1.5)

        # 2. 点击 "添加好友" 图标（Profile 页顶部右上角的 + 或 person_add 图标）
        add_friend_clicked = False
        for sel in [
            {"description": "Add friends"},
            {"descriptionContains": "Add friend"},
            {"descriptionContains": "add friend"},
            {"text": "Add friends"},
            {"resourceId": "com.ss.android.ugc.trill:id/d6s"},
        ]:
            try:
                if d(**sel).exists(timeout=2):
                    d(**sel).click()
                    add_friend_clicked = True
                    break
            except Exception:
                continue

        if not add_friend_clicked:
            # 坐标兜底：右上角添加好友按钮
            px = int(tiktok_automation._screen_w * 0.85)
            py = int(tiktok_automation._screen_h * 0.06)
            hb.tap(d, px, py)
            time.sleep(1)

        time.sleep(2)

        # 3. 切换到 "Contacts" tab
        contacts_tab_clicked = False
        for sel in [
            {"text": "Contacts"},
            {"text": "通讯录"},
            {"text": "Contatti"},
            {"descriptionContains": "Contacts"},
        ]:
            try:
                if d(**sel).exists(timeout=2):
                    d(**sel).click()
                    contacts_tab_clicked = True
                    break
            except Exception:
                continue

        if not contacts_tab_clicked:
            log.warning("[通讯录好友] 未找到 Contacts tab")
            results["errors"].append("Contacts tab not found")
            return results

        time.sleep(2)

        # 4. 处理权限弹窗（同步通讯录权限）
        for perm_text in ["Allow", "ALLOW", "Sync contacts", "Find friends"]:
            try:
                if d(text=perm_text).exists(timeout=1):
                    d(text=perm_text).click()
                    time.sleep(1)
            except Exception:
                pass

        time.sleep(3)

        # 5. 遍历通讯录好友列表
        processed = 0
        seen_names: Set[str] = set()

        for scroll_round in range(5):
            # 获取当前可见的好友条目
            try:
                items = d(className="android.view.ViewGroup",
                          clickable=True).all() or []
            except Exception:
                items = []

            for item in items:
                if processed >= max_friends:
                    break

                try:
                    # 提取名字
                    name_el = item.child(className="android.widget.TextView")
                    if not name_el.exists:
                        continue
                    name = name_el.get_text() or ""
                    if not name or name in seen_names:
                        continue
                    # 跳过非好友 UI 元素
                    if name.lower() in ("contacts", "find friends", "invite",
                                        "suggested", "search"):
                        continue

                    seen_names.add(name)
                    results["found"] += 1

                    # 查找 Follow 按钮
                    if auto_follow:
                        follow_btn = item.child(text="Follow")
                        if not follow_btn.exists:
                            follow_btn = item.child(textContains="Follow")
                        if follow_btn.exists:
                            follow_btn.click()
                            results["followed"] += 1
                            time.sleep(random.uniform(0.8, 1.5))

                    # 发送消息
                    if auto_message:
                        try:
                            from ..ai.chat_bridge import generate_contact_message
                            msg = generate_contact_message(
                                username=name,
                                target_language=target_language,
                                contact_info=contact_info,
                            )
                            if msg:
                                # 点击头像进入资料页 → Message 按钮
                                item.click()
                                time.sleep(1.5)
                                msg_btn = None
                                for sel in [{"text": "Message"}, {"text": "消息"},
                                            {"descriptionContains": "Message"}]:
                                    try:
                                        if d(**sel).exists(timeout=1):
                                            msg_btn = d(**sel)
                                            break
                                    except Exception:
                                        continue
                                if msg_btn:
                                    msg_btn.click()
                                    time.sleep(1)
                                    # 输入框
                                    inp = d(className="android.widget.EditText")
                                    if inp.exists(timeout=2):
                                        inp.set_text(msg)
                                        time.sleep(0.5)
                                        # 发送
                                        for send_sel in [{"description": "Send"},
                                                         {"contentDescription": "Send"},
                                                         {"resourceId": "com.ss.android.ugc.trill:id/send_btn"}]:
                                            try:
                                                if d(**send_sel).exists(timeout=1):
                                                    d(**send_sel).click()
                                                    results["messaged"] += 1
                                                    break
                                            except Exception:
                                                continue
                                    time.sleep(0.5)
                                d.press("back")
                                time.sleep(0.5)
                                d.press("back")
                                time.sleep(0.5)
                        except Exception as e:
                            log.debug(f"[通讯录好友] 发消息失败 {name}: {e}")
                            d.press("back")
                            time.sleep(0.5)

                    # 回写发现结果到数据库
                    _did_follow = results["followed"] > _pre_followed if '_pre_followed' in dir() else False
                    _did_msg = results["messaged"] > _pre_messaged if '_pre_messaged' in dir() else False
                    save_discovery_result(
                        device_id=device_id,
                        contact_name=name,
                        tiktok_username=name,
                        followed=auto_follow,
                        messaged=_did_msg,
                    )
                    results["discovered_names"].append(name)
                    processed += 1

                except Exception as e:
                    log.debug(f"[通讯录好友] 处理条目失败: {e}")
                    continue

            if processed >= max_friends:
                break

            # 向下滚动加载更多
            hb.swipe_up(d, duration=0.5)
            time.sleep(1.5)

    except Exception as e:
        log.error(f"[通讯录好友] 流程失败: {e}")
        results["errors"].append(str(e))

    log.info(f"[通讯录好友] 完成: found={results['found']} followed={results['followed']} messaged={results['messaged']}")
    return results


import random
