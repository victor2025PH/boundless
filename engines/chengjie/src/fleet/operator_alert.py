"""Local operator alert for a room PC (agent 0.3.15).

The fleet service learns which phones cannot work from the same adb inventory
the heartbeat already collects, plus consecutive failed phone actions. This
module turns that into a desktop notice on *this* PC. It does not add fields
to the controller heartbeat and it does not run on a live-stream host.

Phones are shown by wallpaper number (``wallpaper_map`` in agent.json). The
adb serial is never written into the snapshot, the log line, or the window.

The WinForms panel is a separate process on the interactive desktop. The
service often runs as SYSTEM in session 0, which cannot show UI on its own.
Session 0 only writes the snapshot. A logon scheduled task (``ChatX Fleet
Panel``, ONLOGON, interactive) and an HKLM Run value (``ChatXFleetPanel``)
start the same panel inside the logged-on user's session; that process
reads the snapshot. The service asks the logon task to run when a todo
arrives. Chinese / English stays on the panel. If that launch is impossible
the agent keeps running and only writes the snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .phones import is_protected
from .protocol import STATUS_DONE, STATUS_FAILED

logger = logging.getLogger("fleet.operator_alert")

SNAP_NAME = "operator_alert.json"
LANG_NAME = "operator_alert_lang.json"
LOCK_NAME = "operator_alert.lock"
SCRIPT_NAME = "operator_alert_panel.ps1"
PANEL_SESSION_NAME = "panel_session.json"
PANEL_TASK_NAME = "ChatX Fleet Panel"
PANEL_RUN_VALUE = "ChatXFleetPanel"
PANEL_RUN_KEY = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"

_HEADLESS_ENV = "CHATX_OPERATOR_ALERT_HEADLESS"
_LANGS = ("zh", "en")
_SERIAL_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_WALL_RE = re.compile(r"^\d{1,4}$")
_MAX_MAP = 128
_REFRESH_DEFAULT, _REFRESH_MIN, _REFRESH_MAX = 180, 30, 3600
_STREAK_DEFAULT, _STREAK_MIN, _STREAK_MAX = 3, 2, 20

# User-facing copy. The PowerShell panel is ASCII-only and reads these from
# the snapshot, so both languages travel with every refresh and the toggle
# does not wait for the next heartbeat.
STRINGS: Dict[str, Dict[str, str]] = {
    "zh": {
        "title": "机房手机异常",
        "all_clear": "当前没有不能用的手机",
        "pc_line": "这台电脑：{reason}",
        "phone_line": "{no} 号：{reason}",
        "unnumbered": "未编号",
        "pc_name": "这台电脑",
        "toggle": "English",
        "toast_title": "机房手机异常",
        "recovered": "已恢复：{items}",
        "hide_hint": "关闭窗口后留在托盘；有新的异常会再弹出来",
        "refresh_hint": "名单会自动更新",
        "reason_offline": "离线",
        "reason_unauthorized": "未授权",
        "reason_disconnected": "已断开",
        "reason_action_failures": "连续失败 {detail} 次",
        "reason_not_ready": "状态异常（{detail}）",
        "reason_adb_server_down": "adb 服务没在运行",
        "reason_adb_not_found": "找不到 adb",
        "reason_adb_timeout": "adb 超时",
        "reason_adb_version": "adb 版本不一致",
        "reason_adb_error": "adb 出错",
        "reason_no_devices": "没有发现手机",
        "reason_phones_disabled": "手机清点已关闭",
        "title_todo": "机房现场待办",
        "cat_no_network": "没网",
        "act_no_network": "开流量或连WiFi",
        "cat_no_signal": "没信号",
        "act_no_signal": "查SIM或摆位",
        "cat_unauthorized": "未授权",
        "act_unauthorized": "点允许USB调试并贴号",
        "cat_missing_wallpaper": "补壁纸号",
        "act_missing_wallpaper": "补号",
        "cat_usb_unplugged": "USB掉线",
        "act_usb_unplugged": "重插或换线",
        "cat_ledger_conflict": "台账冲突",
        "act_ledger_conflict": "需给其中一部重新编号",
        "cat_fb_logged_out": "Facebook未登录",
        "act_fb_logged_out": "在手机上登录Facebook",
    },
    "en": {
        "title": "Room phone alert",
        "all_clear": "All phones are working",
        "pc_line": "This PC: {reason}",
        "phone_line": "{no}: {reason}",
        "unnumbered": "Unnumbered",
        "pc_name": "This PC",
        "toggle": "中文",
        "toast_title": "Room phone alert",
        "recovered": "Recovered: {items}",
        "hide_hint": "Closing hides this window to the tray. It opens again when a phone fails.",
        "refresh_hint": "This list updates automatically",
        "reason_offline": "Offline",
        "reason_unauthorized": "Unauthorized",
        "reason_disconnected": "Disconnected",
        "reason_action_failures": "Failed {detail} times in a row",
        "reason_not_ready": "Not ready ({detail})",
        "reason_adb_server_down": "adb server is down",
        "reason_adb_not_found": "adb was not found",
        "reason_adb_timeout": "adb timed out",
        "reason_adb_version": "adb version mismatch",
        "reason_adb_error": "adb error",
        "reason_no_devices": "No phones found",
        "reason_phones_disabled": "Phone inventory is off",
        "title_todo": "Room to-do",
        "cat_no_network": "No network",
        "act_no_network": "Turn on mobile data or join Wi-Fi",
        "cat_no_signal": "No signal",
        "act_no_signal": "Check the SIM or move the phone",
        "cat_unauthorized": "Unauthorized",
        "act_unauthorized": "Allow USB debugging and label it",
        "cat_missing_wallpaper": "Missing wallpaper number",
        "act_missing_wallpaper": "Add the number",
        "cat_usb_unplugged": "USB unplugged",
        "act_usb_unplugged": "Reseat or replace the cable",
        "cat_ledger_conflict": "Ledger conflict",
        "act_ledger_conflict": "Renumber one of these phones",
        "cat_fb_logged_out": "Facebook logged out",
        "act_fb_logged_out": "Log in to Facebook on the phone",
    },
}

PANEL_SCRIPT = r"""param(
  [Parameter(Mandatory=$true)][string]$Snapshot,
  [Parameter(Mandatory=$true)][string]$Lang,
  [Parameter(Mandatory=$true)][string]$Lock
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:lockStream = $null
try {
  $script:lockStream = [System.IO.File]::Open(
    $Lock,
    [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None)
} catch {
  exit 0
}

$script:quitting = $false
$script:lastSeq = -1
$script:lang = 'zh'

function Read-Utf8Json([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  $bytes = [System.IO.File]::ReadAllBytes($path)
  if ($bytes.Length -eq 0) { return $null }
  $start = 0
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    $start = 3
  }
  $text = [System.Text.Encoding]::UTF8.GetString($bytes, $start, $bytes.Length - $start)
  if ([string]::IsNullOrWhiteSpace($text)) { return $null }
  return ($text | ConvertFrom-Json)
}

function AsArray($v) {
  $list = New-Object System.Collections.ArrayList
  if ($null -eq $v) { return ,$list.ToArray() }
  if ($v -is [System.Array]) {
    foreach ($x in $v) { [void]$list.Add($x) }
  } else {
    [void]$list.Add($v)
  }
  return ,$list.ToArray()
}

function Txt($snap, $lang, $key) {
  $pack = $null
  if ($null -ne $snap.strings) { $pack = $snap.strings.$lang }
  if ($null -eq $pack -and $null -ne $snap.strings) { $pack = $snap.strings.zh }
  if ($null -eq $pack) { return [string]$key }
  $val = $pack.$key
  if ($null -eq $val) { return [string]$key }
  return [string]$val
}

function Get-Lang($snap) {
  $want = ''
  try {
    $doc = Read-Utf8Json $Lang
    if ($null -ne $doc -and $doc.language) { $want = [string]$doc.language }
  } catch {
    $want = ''
  }
  if ($want -ne 'zh' -and $want -ne 'en') {
    if ($snap.language) { $want = [string]$snap.language }
  }
  if ($want -ne 'zh' -and $want -ne 'en') { $want = 'zh' }
  return $want
}

function ReasonText($snap, $lang, $reason, $detail) {
  $t = Txt $snap $lang ('reason_' + [string]$reason)
  return $t.Replace('{detail}', [string]$detail)
}

function PhoneNo($snap, $lang, $p) {
  $no = ''
  if ($null -ne $p.wallpaper_no) { $no = [string]$p.wallpaper_no }
  $flag = $false
  if ($null -ne $p.unnumbered) { $flag = [bool]$p.unnumbered }
  if ($flag -or [string]::IsNullOrWhiteSpace($no)) {
    $no = Txt $snap $lang 'unnumbered'
    $k = ''
    if ($null -ne $p.key) { $k = [string]$p.key }
    if ($k -match ':(\d+)$') { $no = $no + ' ' + $Matches[1] }
  }
  return $no
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'ChatX'
$form.TopMost = $true
$form.ShowInTaskbar = $true
$form.Size = New-Object System.Drawing.Size(440, 390)
$form.StartPosition = 'CenterScreen'
$form.MinimizeBox = $true
$form.MaximizeBox = $false

$pcLabel = New-Object System.Windows.Forms.Label
$pcLabel.Location = New-Object System.Drawing.Point(12, 10)
$pcLabel.Size = New-Object System.Drawing.Size(400, 44)
$form.Controls.Add($pcLabel)

$list = New-Object System.Windows.Forms.ListBox
$list.Location = New-Object System.Drawing.Point(12, 58)
$list.Size = New-Object System.Drawing.Size(400, 200)
$form.Controls.Add($list)

$hint = New-Object System.Windows.Forms.Label
$hint.Location = New-Object System.Drawing.Point(12, 264)
$hint.Size = New-Object System.Drawing.Size(400, 36)
$form.Controls.Add($hint)

$btn = New-Object System.Windows.Forms.Button
$btn.Location = New-Object System.Drawing.Point(12, 308)
$btn.Size = New-Object System.Drawing.Size(120, 28)
$form.Controls.Add($btn)

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Visible = $true
$notify.Icon = [System.Drawing.SystemIcons]::Warning
$notify.Text = 'ChatX'

function Update-View {
  try {
    $snap = Read-Utf8Json $Snapshot
    if ($null -eq $snap) { return }
    $enabledProp = $snap.PSObject.Properties['enabled']
    if ($null -ne $enabledProp -and -not $enabledProp.Value) {
      $script:quitting = $true
      $form.Close()
      return
    }
    $script:lang = Get-Lang $snap
    $titleKey = 'title'
    $todoProp = $snap.PSObject.Properties['todos']
    if ($null -ne $todoProp) { $titleKey = 'title_todo' }
    $title = Txt $snap $script:lang $titleKey
    $form.Text = $title
    if ($title.Length -gt 63) { $notify.Text = $title.Substring(0, 63) } else { $notify.Text = $title }
    $btn.Text = Txt $snap $script:lang 'toggle'
    $hint.Text = Txt $snap $script:lang 'hide_hint'
    $list.Items.Clear()
    $pcText = ''
    if ($null -ne $snap.pc -and $snap.pc.reason) {
      $why = ReasonText $snap $script:lang $snap.pc.reason ''
      $pcText = (Txt $snap $script:lang 'pc_line').Replace('{reason}', $why)
    }
    $pcLabel.Text = $pcText
    $phones = AsArray $snap.phones
    $network = AsArray $snap.network
    if ($null -ne $todoProp) {
      $todos = AsArray $todoProp.Value
      if ($todos.Length -eq 0 -and [string]::IsNullOrEmpty($pcText)) {
        [void]$list.Items.Add((Txt $snap $script:lang 'all_clear'))
      }
      $order = @('no_network','no_signal','unauthorized','missing_wallpaper','usb_unplugged','ledger_conflict','fb_logged_out')
      foreach ($cat in $order) {
        $rows = New-Object System.Collections.ArrayList
        foreach ($t in $todos) {
          $c = ''
          if ($null -ne $t.category) { $c = [string]$t.category }
          if ($c -eq $cat) { [void]$rows.Add($t) }
        }
        if ($rows.Count -eq 0) { continue }
        [void]$list.Items.Add((Txt $snap $script:lang ('cat_' + $cat)))
        $action = Txt $snap $script:lang ('act_' + $cat)
        foreach ($t in $rows) {
          $no = ''
          if ($null -ne $t.wallpaper_no) { $no = [string]$t.wallpaper_no }
          $flag = $false
          if ($null -ne $t.unnumbered) { $flag = [bool]$t.unnumbered }
          if ($flag -or [string]::IsNullOrWhiteSpace($no)) {
            $no = Txt $snap $script:lang 'unnumbered'
            if ($null -ne $t.slot) { $no = $no + ' ' + [string]$t.slot }
          }
          $line = (Txt $snap $script:lang 'phone_line').Replace('{no}', $no).Replace('{reason}', $action)
          [void]$list.Items.Add('  ' + $line)
        }
      }
    } else {
      if ($phones.Length -eq 0 -and $network.Length -eq 0 -and [string]::IsNullOrEmpty($pcText)) {
        [void]$list.Items.Add((Txt $snap $script:lang 'all_clear'))
      }
      foreach ($p in $phones) {
        $no = PhoneNo $snap $script:lang $p
        $detail = ''
        if ($null -ne $p.detail) { $detail = [string]$p.detail }
        $why = ReasonText $snap $script:lang $p.reason $detail
        $line = (Txt $snap $script:lang 'phone_line').Replace('{no}', $no).Replace('{reason}', $why)
        [void]$list.Items.Add($line)
      }
      foreach ($n in $network) {
        $no = PhoneNo $snap $script:lang $n
        $status = ''
        if ($script:lang -eq 'en') {
          if ($null -ne $n.status_en) { $status = [string]$n.status_en }
        } elseif ($null -ne $n.status_zh) {
          $status = [string]$n.status_zh
        }
        if ([string]::IsNullOrEmpty($status)) { continue }
        $line = (Txt $snap $script:lang 'phone_line').Replace('{no}', $no).Replace('{reason}', $status)
        [void]$list.Items.Add($line)
      }
    }
    $seq = 0
    if ($null -ne $snap.seq) { $seq = [int]$snap.seq }
    if ($seq -ne $script:lastSeq) {
      $raised = AsArray $snap.raised
      $cleared = AsArray $snap.cleared
      if ($raised.Length -gt 0) {
        $form.Show()
        $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
        $form.Activate()
        $tip = @()
        if (-not [string]::IsNullOrEmpty($pcText)) { $tip += $pcText }
        foreach ($item in $list.Items) { $tip += [string]$item }
        $text = ($tip -join ' | ')
        if ($text.Length -gt 180) { $text = $text.Substring(0, 180) }
        $notify.BalloonTipTitle = Txt $snap $script:lang 'toast_title'
        $notify.BalloonTipText = $text
        $notify.ShowBalloonTip(8000)
      } elseif ($cleared.Length -gt 0) {
        $names = @()
        foreach ($c in $cleared) {
          $name = [string]$c
          if ($name -eq 'pc') { $name = Txt $snap $script:lang 'pc_name' }
          $names += $name
        }
        $items = ($names -join ', ')
        $notify.BalloonTipTitle = Txt $snap $script:lang 'toast_title'
        $notify.BalloonTipText = (Txt $snap $script:lang 'recovered').Replace('{items}', $items)
        $notify.ShowBalloonTip(5000)
      }
      $script:lastSeq = $seq
    }
  } catch {
  }
}

$btn.add_Click({
  try {
    $next = 'en'
    if ($script:lang -eq 'en') { $next = 'zh' }
    $json = '{"language":"' + $next + '"}'
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Lang, $json, $utf8)
    $script:lang = $next
    Update-View
  } catch {
  }
})

$form.add_FormClosing({
  param($sender, $e)
  if (-not $script:quitting) {
    $e.Cancel = $true
    $form.Hide()
  }
})

$notify.add_DoubleClick({
  $form.Show()
  $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
  $form.Activate()
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.add_Tick({ Update-View })
$timer.Start()
Update-View
if (-not $script:quitting) {
  [void][System.Windows.Forms.Application]::Run($form)
}
$timer.Stop()
$notify.Visible = $false
$notify.Dispose()
if ($null -ne $script:lockStream) { $script:lockStream.Dispose() }
"""


_DIAG_REASONS = frozenset({
    "offline", "unauthorized", "disconnected", "action_failures", "not_ready",
    "adb_server_down", "adb_not_found", "adb_timeout", "adb_version", "adb_error",
    "no_devices", "phones_disabled",
})


def operator_alert_diag(state_dir: Path, cfg_data: Any, *, live_stream: bool = False) -> Dict[str, Any]:
    """Redacted summary for a remote read. No adb serials and no string packs.

    Settings come from agent.json (language sidecar still wins). The phone list
    comes from the snapshot and keeps only wallpaper number and reason. A
    live-stream host reports the alert off and an empty list.
    """
    cfg = parse_alert_config(cfg_data)
    root = Path(state_dir)
    language = resolve_language(root, cfg["language"])
    snap = _load_snapshot(root)
    phones: List[Dict[str, Any]] = []
    pc_reason = ""
    if isinstance(snap, dict) and not live_stream:
        raw_phones = snap.get("phones")
        if isinstance(raw_phones, list):
            for item in raw_phones:
                if not isinstance(item, dict):
                    continue
                number = str(item.get("wallpaper_no") or "").strip()
                if not _WALL_RE.match(number) or int(number) < 1:
                    number = ""
                reason = str(item.get("reason") or "").strip()
                if reason not in _DIAG_REASONS:
                    reason = ""
                row: Dict[str, Any] = {"wallpaper_no": number, "reason": reason}
                if item.get("unnumbered") is True or not number:
                    row["unnumbered"] = True
                phones.append(row)
                if len(phones) >= _MAX_MAP:
                    break
        pc = snap.get("pc")
        if isinstance(pc, dict):
            reason = str(pc.get("reason") or "").strip()
            if reason in _DIAG_REASONS:
                pc_reason = reason
    network: List[Dict[str, Any]] = []
    if isinstance(snap, dict) and not live_stream and isinstance(snap.get("network"), list):
        from .net_health import _STATUS_EN, _STATUS_ZH

        for item in snap["network"]:
            if not isinstance(item, dict):
                continue
            number = str(item.get("wallpaper_no") or "").strip()
            if not _WALL_RE.match(number) or int(number) < 1:
                number = ""
            zh = item.get("status_zh") if item.get("status_zh") in _STATUS_ZH else ""
            en = item.get("status_en") if item.get("status_en") in _STATUS_EN else ""
            row = {"wallpaper_no": number, "status_zh": zh, "status_en": en}
            if item.get("unnumbered") is True or not number:
                row["unnumbered"] = True
            network.append(row)
            if len(network) >= _MAX_MAP:
                break
    todos: List[Dict[str, Any]] = []
    if isinstance(snap, dict) and not live_stream and isinstance(snap.get("todos"), list):
        from .site_todo import sanitize_site_todos

        todos = sanitize_site_todos(snap.get("todos"))
    lock = root / LOCK_NAME
    panel_running = (not live_stream) and lock.is_file() and _lock_held(lock)
    out: Dict[str, Any] = {
        "enabled": bool(cfg["enabled"]) and not live_stream,
        "language": language,
        "refresh_sec": cfg["refresh_sec"],
        "fail_streak": cfg["fail_streak"],
        "live_stream": bool(live_stream),
        "snapshot": isinstance(snap, dict) and not live_stream,
        "phones": phones,
        "todos": todos,
        "panel_running": panel_running,
        "last_present": _present_view(None if live_stream else snap),
        "session_id": -1 if live_stream else _session_view(snap),
    }
    panel = {"task_account": "", "run_key_present": False, "panel_session_id": -1}
    if not live_stream:
        panel = panel_runtime_diag(root)
    out["task_account"] = panel["task_account"]
    out["run_key_present"] = panel["run_key_present"]
    out["panel_session_id"] = panel["panel_session_id"]
    if network:
        out["network"] = network
    if pc_reason:
        out["pc_reason"] = pc_reason
    return out


_PRESENT_MODES = frozenset({"disabled", "headless", "already_running", "gui"})


def _present_view(snap: Any) -> Dict[str, Any]:
    default = {"ok": False, "mode": "headless", "shown": False}
    if not isinstance(snap, dict):
        return default
    raw = snap.get("present")
    if not isinstance(raw, dict):
        return default
    mode = raw.get("mode")
    if mode not in _PRESENT_MODES:
        return default
    return {"ok": raw.get("ok") is True, "mode": mode, "shown": raw.get("shown") is True}


def _session_view(snap: Any) -> int:
    if isinstance(snap, dict):
        sid = snap.get("session_id")
        if isinstance(sid, int) and not isinstance(sid, bool) and -1 <= sid <= 10_000_000:
            return sid
    return _current_session_id()


def _load_snapshot(state_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads((Path(state_dir) / SNAP_NAME).read_text(encoding="utf-8-sig"))
    except Exception:
        # Missing or unreadable snapshot is "no notice yet", but it must not be silent:
        # a corrupt file otherwise looks the same as a fresh state dir.
        logger.debug("[operator_alert] snapshot unreadable %s", state_dir, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def parse_alert_config(data: Any) -> Dict[str, Any]:
    """Flat agent.json keys. ``operator_alert_enabled`` is on only for JSON true."""
    src = data if isinstance(data, dict) else {}
    enabled = src.get("operator_alert_enabled") is True
    refresh = _clamp_int(src.get("operator_alert_refresh_sec"), _REFRESH_DEFAULT, _REFRESH_MIN, _REFRESH_MAX)
    language = str(src.get("operator_alert_language") or "zh").strip().lower()
    if language not in _LANGS:
        language = "zh"
    streak = _clamp_int(src.get("operator_alert_fail_streak"), _STREAK_DEFAULT, _STREAK_MIN, _STREAK_MAX)
    return {"enabled": enabled, "refresh_sec": refresh, "language": language, "fail_streak": streak}


def parse_wallpaper_map(raw: Any) -> Dict[str, str]:
    """serial (upper) -> wallpaper number text. Bad rows are ignored.

    Accepted shapes: ``{"7": "SERIAL"}``, ``{"SERIAL": 7}``, ``{"SERIAL": "07"}``,
    or ``[{"wallpaper_no": "07", "serial": "SERIAL"}]``. Leading zeros on a
    string number are kept. Protected serials are dropped.
    """
    pairs: List[Tuple[str, str]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            ks = str(key).strip()
            if _WALL_RE.match(ks) and int(ks) >= 1:
                pairs.append((str(value).strip(), ks))
            else:
                pairs.append((ks, _wallpaper_label(value)))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            pairs.append((str(item.get("serial") or "").strip(), _wallpaper_label(item.get("wallpaper_no"))))
    out: Dict[str, str] = {}
    for serial, number in pairs:
        su = serial.strip().upper()
        if not number or not _SERIAL_RE.match(su) or is_protected(su) or su in out:
            continue
        out[su] = number
        if len(out) >= _MAX_MAP:
            break
    return out


def load_language(state_dir: Path) -> Optional[str]:
    try:
        raw = json.loads((Path(state_dir) / LANG_NAME).read_text(encoding="utf-8-sig"))
    except Exception:
        # No sidecar yet falls through to agent.json. A broken sidecar used to
        # do the same with no trace, so a bad write was indistinguishable.
        logger.debug("[operator_alert] language file unreadable %s", state_dir, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    lang = str(raw.get("language") or "").strip().lower()
    return lang if lang in _LANGS else None


def save_language(state_dir: Path, language: str) -> None:
    lang = language if language in _LANGS else "zh"
    _atomic_write(Path(state_dir) / LANG_NAME, json.dumps({"language": lang}, ensure_ascii=False))


def resolve_language(state_dir: Path, default: str) -> str:
    """Sidecar written by the panel toggle wins over agent.json."""
    side = load_language(state_dir)
    if side in _LANGS:
        return side
    return default if default in _LANGS else "zh"


def gui_available() -> bool:
    """False in tests, when the operator forces headless, and off Windows."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    flag = os.environ.get(_HEADLESS_ENV, "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    return os.name == "nt"


class OperatorAlert:
    """In-memory streaks plus the snapshot written for the desktop panel.

    ``observe`` and ``note_action`` never raise.
    """

    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time,
                 live_stream: bool = False, present: Optional[Callable[..., Any]] = None) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.live_stream = bool(live_stream)
        self.present = present or present_operator_alert
        self._streaks: Dict[str, int] = {}
        self._seen_device: set = set()
        self._active_since: Dict[str, float] = {}
        self._active_serial: Dict[str, str] = {}
        self._active_sig: Dict[str, str] = {}
        self._slots: Dict[str, int] = {}
        self._next_slot = 1
        self._seq = 0
        self._last_write = 0.0
        self._ever_enabled = False
        self._network: List[Dict[str, Any]] = []
        self._net_bad: set = set()
        self._last_phones: List[Dict[str, Any]] = []
        self._last_pc: Optional[Dict[str, Any]] = None
        self._todos: Optional[List[Dict[str, Any]]] = None
        self._hold_present = False
        self._restore()

    def note_action(self, serial: str, status: str) -> None:
        try:
            if self.live_stream:
                return
            su = str(serial or "").strip().upper()
            if not su or not _SERIAL_RE.match(su) or is_protected(su):
                return
            if status == STATUS_FAILED:
                self._streaks[su] = min(self._streaks.get(su, 0) + 1, 1000)
            elif status == STATUS_DONE:
                self._streaks[su] = 0
        except Exception:
            logger.debug("[operator_alert] note_action failed", exc_info=True)

    def observe(self, phones: Any, phones_error: Any, cfg_data: Any, now: Optional[float] = None) -> None:
        try:
            self._observe(phones, phones_error, cfg_data, now)
        except Exception:
            logger.warning("[operator_alert] observe skipped", exc_info=True)

    def _observe(self, phones: Any, phones_error: Any, cfg_data: Any, now: Optional[float]) -> None:
        if self.live_stream:
            return
        cfg = parse_alert_config(cfg_data)
        if not cfg["enabled"]:
            if self._ever_enabled:
                self._seq += 1
                self._ever_enabled = False
                self._active_since.clear()
                self._active_serial.clear()
                self._active_sig.clear()
                self._last_write = self.clock() if now is None else float(now)
                self.present(self.state_dir, {"enabled": False, "seq": self._seq})
            return
        stamp = float(self.clock() if now is None else now)
        wall = parse_wallpaper_map((cfg_data or {}).get("wallpaper_map") if isinstance(cfg_data, dict) else None)
        current = _index_phones(phones)
        for serial, state in current.items():
            if state == "device":
                self._seen_device.add(serial)
        err = str(phones_error or "").strip()
        healthy = err == ""
        problems = _problems(current, self._streaks, cfg["fail_streak"])
        if healthy:
            present = set(current)
            watched = set(wall) | set(self._seen_device) | set(self._active_serial.values())
            for serial in sorted(watched - present):
                if serial in {p[0] for p in problems}:
                    continue
                if not _SERIAL_RE.match(serial) or is_protected(serial):
                    continue
                problems.append((serial, "disconnected", ""))
        pc = _pc_reason(err, list(current.values()))
        items = self._public_items(problems, wall, stamp)
        new_keys = [it["key"] for it in items]
        if pc:
            new_keys = ["pc"] + new_keys
        new_set = set(new_keys)
        raised = [k for k in new_keys if k not in self._active_since]
        cleared = [k for k in list(self._active_since) if k not in new_set]
        changed = bool(raised or cleared)
        sigs = {item["key"]: f"{item['reason']}|{item['detail']}" for item in items}
        if pc:
            sigs["pc"] = pc
        updated = any(self._active_sig.get(key) != sig for key, sig in sigs.items() if key in self._active_since)
        if not changed and not updated and not new_set:
            return
        if not changed and not updated and (stamp - self._last_write) < cfg["refresh_sec"]:
            return
        for key in cleared:
            serial = self._active_serial.pop(key, "")
            self._active_since.pop(key, None)
            if serial and serial not in {it["_serial"] for it in items}:
                self._slots.pop(serial, None)
            self._active_sig.pop(key, None)
        for item in items:
            key = item["key"]
            if key not in self._active_since:
                self._active_since[key] = stamp
            self._active_serial[key] = item["_serial"]
            item["since"] = self._active_since[key]
            item["age_sec"] = max(0, int(stamp - item["since"]))
            self._active_sig[key] = sigs[key]
        if pc:
            if "pc" not in self._active_since:
                self._active_since["pc"] = stamp
            self._active_sig["pc"] = pc
            pc_obj: Optional[Dict[str, Any]] = {
                "key": "pc", "reason": pc, "since": self._active_since["pc"],
                "age_sec": max(0, int(stamp - self._active_since["pc"])),
            }
        else:
            self._active_since.pop("pc", None)
            self._active_sig.pop("pc", None)
            pc_obj = None
        if not changed:
            raised = []
            cleared = []
        public_phones = []
        for item in items:
            public_phones.append({k: v for k, v in item.items() if k != "_serial"})
        self._seq += 1
        self._last_write = stamp
        self._ever_enabled = True
        self._last_phones = public_phones
        self._last_pc = pc_obj
        language = resolve_language(self.state_dir, cfg["language"])
        payload = {
            "enabled": True,
            "seq": self._seq,
            "language": language,
            "refresh_sec": cfg["refresh_sec"],
            "updated_at": stamp,
            "pc": pc_obj,
            "phones": public_phones,
            "network": [dict(item) for item in self._network],
            "raised": raised,
            "cleared": cleared,
            "strings": STRINGS,
        }
        self._attach_todos(payload)
        if self._hold_present and not raised:
            _atomic_write(self.state_dir / SNAP_NAME, json.dumps(payload, ensure_ascii=False))
        else:
            if raised:
                self._hold_present = False
            self.present(self.state_dir, payload)

    def note_network(self, rows: Any, cfg_data: Any) -> None:
        """Remember per-phone network lines and refresh the local snapshot.

        A newly bad phone raises the window. A healthy refresh only rewrites
        the snapshot, so an already-open window updates and a quiet room does
        not pop. Serials are dropped before the write.
        """
        try:
            self._note_network(rows, cfg_data)
        except Exception:
            logger.warning("[operator_alert] note_network skipped", exc_info=True)

    def _note_network(self, rows: Any, cfg_data: Any) -> None:
        if self.live_stream:
            return
        from .net_health import popup_row

        public: List[Dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, dict):
                public.append(popup_row(row))
            if len(public) >= 32:
                break
        self._network = public
        cfg = parse_alert_config(cfg_data)
        if not cfg["enabled"]:
            return
        bad = [item["key"] for item in public if item.get("alert") and item.get("key")]
        raised = [key for key in bad if key not in self._net_bad]
        self._net_bad = set(bad)
        stamp = float(self.clock())
        self._seq += 1
        self._last_write = stamp
        self._ever_enabled = True
        language = resolve_language(self.state_dir, cfg["language"])
        payload = {
            "enabled": True,
            "seq": self._seq,
            "language": language,
            "refresh_sec": cfg["refresh_sec"],
            "updated_at": stamp,
            "pc": self._last_pc,
            "phones": [dict(item) for item in self._last_phones],
            "network": public,
            "raised": raised,
            "cleared": [],
            "strings": STRINGS,
        }
        self._attach_todos(payload)
        if raised:
            self._hold_present = False
            self.present(self.state_dir, payload)
            return
        _atomic_write(self.state_dir / SNAP_NAME, json.dumps(payload, ensure_ascii=False))

    def _public_items(self, problems: List[Tuple[str, str, str]], wall: Dict[str, str],
                      stamp: float) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        used: set = set()
        for serial, reason, detail in problems:
            number = wall.get(serial, "")
            unnumbered = not number
            if number:
                base = number
            else:
                slot = self._slots.get(serial)
                if slot is None:
                    slot = self._next_slot
                    self._next_slot += 1
                    self._slots[serial] = slot
                base = f"unnumbered:{slot}"
            key = base
            n = 2
            while key in used:
                key = f"{base}#{n}"
                n += 1
            used.add(key)
            items.append({
                "key": key,
                "wallpaper_no": number,
                "unnumbered": unnumbered,
                "reason": reason,
                "detail": detail,
                "since": stamp,
                "age_sec": 0,
                "_serial": serial,
            })
        items.sort(key=_phone_sort)
        return items

    def apply_todos(self, todos: Any, cfg_data: Any) -> None:
        """Write the controller's site list and show the panel when it changed.

        A live-stream host does nothing. The same list after a restart does not
        call ``present`` again; a new list does.
        """
        try:
            if self.live_stream:
                return
            from .site_todo import sanitize_site_todos, site_todo_signature

            clean = sanitize_site_todos(todos)
            changed = site_todo_signature(self._todos or []) != site_todo_signature(clean) and not (
                self._todos is None and not clean
            )
            if self._todos is None and clean:
                changed = True
            self._todos = clean
            cfg = parse_alert_config(cfg_data)
            if cfg["enabled"]:
                self._ever_enabled = True
            stamp = float(self.clock())
            self._seq += 1
            self._last_write = stamp
            language = resolve_language(self.state_dir, cfg["language"])
            payload = {
                "enabled": True,
                "seq": self._seq,
                "language": language,
                "refresh_sec": cfg["refresh_sec"],
                "updated_at": stamp,
                "pc": self._last_pc,
                "phones": [dict(item) for item in self._last_phones],
                "network": [dict(item) for item in self._network],
                "raised": ["todos"] if changed else [],
                "cleared": [],
                "strings": STRINGS,
            }
            self._attach_todos(payload)
            if changed:
                self._hold_present = False
                self.present(self.state_dir, payload)
                return
            _atomic_write(self.state_dir / SNAP_NAME, json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.warning("[operator_alert] apply_todos skipped", exc_info=True)

    def _attach_todos(self, payload: Dict[str, Any]) -> None:
        if self._todos is not None:
            payload["todos"] = [dict(item) for item in self._todos]

    def _restore(self) -> None:
        """Keep network rows and site todos across a process restart. Does not show the window."""
        if self.live_stream:
            return
        snap = _load_snapshot(self.state_dir)
        if not isinstance(snap, dict):
            return
        self._hold_present = True
        updated = snap.get("updated_at")
        if isinstance(updated, (int, float)) and not isinstance(updated, bool):
            self._last_write = float(updated)
        seq = snap.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            self._seq = seq
        network = snap.get("network")
        if isinstance(network, list):
            restored = []
            for row in network:
                if isinstance(row, dict):
                    restored.append({k: v for k, v in row.items() if k not in {"serial", "_serial"}})
                if len(restored) >= 32:
                    break
            self._network = restored
            self._net_bad = {str(item.get("key")) for item in restored if item.get("alert") and item.get("key")}
        phones = snap.get("phones")
        if isinstance(phones, list):
            self._last_phones = []
            for item in phones:
                if not isinstance(item, dict):
                    continue
                public = {k: v for k, v in item.items() if k not in {"serial", "_serial"}}
                self._last_phones.append(public)
                key = str(public.get("key") or "")
                if not key:
                    continue
                since = public.get("since")
                self._active_since[key] = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else self._last_write
                self._active_sig[key] = f"{public.get('reason')}|{public.get('detail') or ''}"
                match = re.search(r"unnumbered:(\d+)", key)
                if match:
                    slot = int(match.group(1))
                    if slot >= self._next_slot:
                        self._next_slot = slot + 1
                if len(self._last_phones) >= _MAX_MAP:
                    break
        pc = snap.get("pc")
        if isinstance(pc, dict):
            self._last_pc = {k: v for k, v in pc.items() if k not in {"serial", "_serial"}}
            if self._last_pc.get("reason"):
                since = self._last_pc.get("since")
                self._active_since["pc"] = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else self._last_write
                self._active_sig["pc"] = str(self._last_pc.get("reason") or "")
        if isinstance(snap.get("todos"), list):
            from .site_todo import sanitize_site_todos

            self._todos = sanitize_site_todos(snap.get("todos"))


def present_operator_alert(state_dir: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write the snapshot, then try to show the panel. Never raises."""
    try:
        state_dir = Path(state_dir)
        body = dict(payload)
        _atomic_write(state_dir / SNAP_NAME, json.dumps(body, ensure_ascii=False))
        if body.get("enabled") is False:
            result = {"ok": True, "mode": "disabled", "shown": False}
        elif not gui_available():
            pc = body.get("pc") if isinstance(body.get("pc"), dict) else {}
            logger.info("[operator_alert] headless non_working=%d pc=%s",
                        len(body.get("phones") or []), str((pc or {}).get("reason") or ""))
            result = {"ok": True, "mode": "headless", "shown": False}
        else:
            try:
                result = launch_panel(state_dir)
            except Exception:
                logger.warning("[operator_alert] panel unavailable; staying headless", exc_info=True)
                result = {"ok": True, "mode": "headless", "shown": False}
        body["present"] = {key: result[key] for key in ("ok", "mode", "shown")}
        body["session_id"] = _current_session_id()
        _atomic_write(state_dir / SNAP_NAME, json.dumps(body, ensure_ascii=False))
        return result
    except Exception:
        logger.warning("[operator_alert] present failed", exc_info=True)
        return {"ok": False, "mode": "headless", "shown": False}


def launch_panel(state_dir: Path) -> Dict[str, Any]:
    """Start the panel if it is not already holding the lock. Never raises."""
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        script = state_dir / SCRIPT_NAME
        _ensure_script(script)
        lock = state_dir / LOCK_NAME
        if _lock_held(lock):
            return {"ok": True, "mode": "already_running", "shown": True}
        if not _spawn_interactive(script, state_dir / SNAP_NAME, state_dir / LANG_NAME, lock):
            return {"ok": True, "mode": "headless", "shown": False}
        return {"ok": True, "mode": "gui", "shown": True}
    except Exception:
        logger.warning("[operator_alert] panel launch failed", exc_info=True)
        return {"ok": True, "mode": "headless", "shown": False}


def _problems(current: Dict[str, str], streaks: Dict[str, int], threshold: int) -> List[Tuple[str, str, str]]:
    found: List[Tuple[str, str, str]] = []
    for serial in sorted(current):
        state = current[serial]
        if state != "device":
            found.append((serial, *_state_reason(state)))
            continue
        streak = int(streaks.get(serial) or 0)
        if streak >= threshold:
            found.append((serial, "action_failures", str(streak)))
    return found


def _index_phones(phones: Any) -> Dict[str, str]:
    current: Dict[str, str] = {}
    if not isinstance(phones, list):
        return current
    for raw in phones:
        if not isinstance(raw, dict):
            continue
        serial = str(raw.get("serial") or "").strip().upper()
        if not _SERIAL_RE.match(serial) or is_protected(serial):
            continue
        current[serial] = str(raw.get("state") or "").strip().lower()
    return current


def _state_reason(state: str) -> Tuple[str, str]:
    if state == "offline":
        return "offline", ""
    if state in {"unauthorized", "no_permissions"}:
        return "unauthorized", ""
    return "not_ready", _detail_token(state)


def _pc_reason(error: str, states: List[str]) -> str:
    err = (error or "").strip()
    if not err:
        return "" if states else "no_devices"
    if err == "disabled":
        return "phones_disabled"
    if err == "adb_not_found":
        return "adb_not_found"
    if err == "adb_server_not_running":
        return "adb_server_down"
    if err == "adb_timeout":
        return "adb_timeout"
    if err == "adb_version_unknown" or err.startswith("adb_version_mismatch"):
        return "adb_version"
    return "adb_error"


def _phone_sort(item: Dict[str, Any]) -> Tuple[int, int, str]:
    if item.get("unnumbered"):
        tail = str(item.get("key") or "").rsplit(":", 1)[-1]
        try:
            slot = int(tail)
        except ValueError:
            slot = 0
        return (1, slot, str(item.get("key") or ""))
    try:
        number = int(str(item.get("wallpaper_no") or "0"))
    except ValueError:
        number = 0
    return (0, number, str(item.get("wallpaper_no") or ""))


def _detail_token(value: str) -> str:
    out: List[str] = []
    for ch in str(value or ""):
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        if len(out) >= 32:
            break
    return "".join(out)


def _wallpaper_label(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        if 1 <= value <= 9999:
            return str(value)
        return ""
    text = str(value).strip()
    if _WALL_RE.match(text) and int(text) >= 1:
        return text
    return ""


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _ensure_script(path: Path) -> None:
    if not PANEL_SCRIPT.isascii():
        raise RuntimeError("operator alert panel script must stay ASCII")
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == PANEL_SCRIPT:
            return
    except OSError:
        pass
    _atomic_write(path, PANEL_SCRIPT)


def _lock_held(lock: Path) -> bool:
    """True when the panel already holds the lock with no sharing."""
    try:
        handle = open(lock, "a+b")
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                return False
            return False
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _state_path(state_dir: Path) -> Path:
    """Keep the caller's path object.

    ``Path()`` follows ``os.name``. A Linux test that only flips ``os.name``
    to exercise schtasks cannot construct a ``WindowsPath``.
    """
    if isinstance(state_dir, Path):
        return state_dir
    return Path(state_dir)


def panel_process_args(state_dir: Path) -> List[str]:
    """PowerShell argv that reads the snapshot on the interactive desktop."""
    root = _state_path(state_dir)
    return [
        _powershell(), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", str(root / SCRIPT_NAME),
        "-Snapshot", str(root / SNAP_NAME), "-Lang", str(root / LANG_NAME),
        "-Lock", str(root / LOCK_NAME),
    ]


def _spawn_interactive(script: Path, snapshot: Path, lang: Path, lock: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        args = [
            _powershell(), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script),
            "-Snapshot", str(snapshot), "-Lang", str(lang), "-Lock", str(lock),
        ]
        session_id = _current_session_id()
        if session_id > 0:
            subprocess.Popen(args, creationflags=0x08000000)
            _remember_panel_session(snapshot, session_id)
            return True
        # Session 0 cannot paint. CreateProcessAsUser on the active console
        # is the launch that counts. schtasks /Run returning 0 from session 0
        # is not proof the panel is on a desktop.
        if _spawn_as_user(args):
            console = _active_console_session_id()
            if console > 0:
                _remember_panel_session(snapshot, console)
            return True
        _kick_logon_panel()
        return _stored_panel_session(snapshot.parent) > 0
    except Exception:
        logger.warning("[operator_alert] panel launch failed", exc_info=True)
        return False


def _kick_logon_panel() -> bool:
    """Ask the user-session logon task to start. Does not create a process here."""
    if os.name != "nt":
        return False
    try:
        from .service import PANEL_TASK_NAME, build_schtasks_run

        proc = subprocess.run(
            build_schtasks_run(task_name=PANEL_TASK_NAME),
            capture_output=True, timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        logger.warning("[operator_alert] logon panel kick failed", exc_info=True)
        return False


def _powershell() -> str:
    windir = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    return "powershell.exe"


def _remember_panel_session(snapshot: Path, session_id: int) -> None:
    """Record the console session the panel was started in. An integer only."""
    if isinstance(session_id, bool) or not isinstance(session_id, int):
        return
    if not 0 <= session_id <= 10_000_000:
        return
    try:
        parent = snapshot.parent if isinstance(snapshot, Path) else Path(snapshot).parent
        _atomic_write(parent / PANEL_SESSION_NAME, json.dumps({"session_id": session_id}))
    except OSError:
        logger.debug("[operator_alert] panel session was not recorded", exc_info=True)


def _stored_panel_session(state_dir: Path) -> int:
    """Panel process session, or -1 when the panel is not holding the lock.

    Session 0 is returned as 0 so a diagnostic can show it. Callers that
    decide ``shown`` treat only a session above 0 as a desktop.
    """
    root = state_dir if isinstance(state_dir, Path) else Path(state_dir)
    lock = root / LOCK_NAME
    if not (lock.is_file() and _lock_held(lock)):
        return -1
    try:
        raw = json.loads((root / PANEL_SESSION_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return -1
    if not isinstance(raw, dict):
        return -1
    sid = raw.get("session_id")
    if isinstance(sid, bool) or not isinstance(sid, int) or not 0 <= sid <= 10_000_000:
        return -1
    return sid


def _safe_task_account(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 80:
        return ""
    folded = text.casefold()
    if folded in {"system", "nt authority\\system"} or text == "S-1-5-18":
        return "SYSTEM"
    if folded in {"interactive", "nt authority\\interactive"} or "s-1-5-4" in folded:
        return "INTERACTIVE"
    if not re.fullmatch(r"[A-Za-z0-9_.\\ @-]{1,80}", text):
        return ""
    # A long token with no domain separator is treated as a serial, not an account.
    if "\\" not in text and len(text) >= 8 and text.isalnum():
        return ""
    return text


def parse_panel_task_account(xml: str) -> str:
    """INTERACTIVE when the task principal is group S-1-5-4, else SYSTEM or ""."""
    if not isinstance(xml, str) or not xml:
        return ""
    if "S-1-5-4" in xml and "GroupId" in xml:
        return "INTERACTIVE"
    if ">SYSTEM<" in xml or "S-1-5-18" in xml:
        return "SYSTEM"
    return ""


def parse_schtasks_account(text: str) -> str:
    """Run-as account from ``schtasks /Query /FO LIST /V``. No other fields."""
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        if key.strip().casefold() not in {"run as user", "作为用户运行"}:
            continue
        return _safe_task_account(val.strip())
    return ""


def _panel_task_account(state_dir: Path) -> str:
    xml_account = ""
    try:
        from .service import PANEL_TASK_XML_NAME

        xml_account = parse_panel_task_account(
            (state_dir / PANEL_TASK_XML_NAME).read_text(encoding="utf-8")
            if isinstance(state_dir, Path) else (Path(state_dir) / PANEL_TASK_XML_NAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError):
        xml_account = ""
    queried = ""
    if os.name == "nt":
        queried = parse_schtasks_account(_query_panel_task_text())
    if queried == "SYSTEM":
        return "SYSTEM"
    if queried:
        return queried
    return xml_account


def _query_panel_task_text() -> str:
    if os.name != "nt":
        return ""
    try:
        from .service import PANEL_TASK_NAME, build_schtasks_query

        proc = subprocess.run(
            build_schtasks_query(task_name=PANEL_TASK_NAME),
            capture_output=True, timeout=15,
        )
    except Exception:
        logger.debug("[operator_alert] panel task query failed", exc_info=True)
        return ""
    raw = getattr(proc, "stdout", b"") or b""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _run_key_present() -> bool:
    """True when the HKLM Run value exists. The command line is not returned."""
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["reg", "query", PANEL_RUN_KEY, "/v", PANEL_RUN_VALUE],
            capture_output=True, timeout=10,
        )
        return int(getattr(proc, "returncode", 1) or 0) == 0
    except Exception:
        logger.debug("[operator_alert] run key query failed", exc_info=True)
        return False


def panel_runtime_diag(state_dir: Path) -> Dict[str, Any]:
    """Task account, Run key, and the panel's session. No serials and no secrets."""
    root = state_dir if isinstance(state_dir, Path) else Path(state_dir)
    account = _panel_task_account(root)
    if account not in {"INTERACTIVE", "SYSTEM", ""}:
        account = _safe_task_account(account)
    return {
        "task_account": account if isinstance(account, str) else "",
        "run_key_present": _run_key_present() is True,
        "panel_session_id": _stored_panel_session(root),
    }


def _active_console_session_id() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        sid = int(kernel32.WTSGetActiveConsoleSessionId() or 0)
        if sid == 0xFFFFFFFF:
            return 0
        return sid
    except Exception:
        return 0


def _current_session_id() -> int:
    if os.name != "nt":
        return -1
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        sid = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
            return -1
        return int(sid.value)
    except Exception:
        return -1


def _spawn_as_user(args: List[str]) -> bool:
    """Session 0 (SYSTEM service) -> interactive console desktop. Failure is headless."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
    except Exception:
        return False

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    try:
        kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        wtsapi32.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
        wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL
        advapi32.DuplicateTokenEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.DuplicateTokenEx.restype = wintypes.BOOL
        userenv.CreateEnvironmentBlock.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL,
        ]
        userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
        userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
        userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
        kernel32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
            ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
        ]
        kernel32.CreateProcessAsUserW.restype = wintypes.BOOL
        session_id = int(kernel32.WTSGetActiveConsoleSessionId() or 0)
        if session_id <= 0:
            return False
        token = wintypes.HANDLE()
        if not wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(token)):
            return False
    except Exception:
        logger.warning("[operator_alert] interactive launch failed", exc_info=True)
        return False

    dup = wintypes.HANDLE()
    env = ctypes.c_void_p()
    try:
        desired = 0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100
        if not advapi32.DuplicateTokenEx(token, desired, None, 2, 1, ctypes.byref(dup)):
            return False
        if not userenv.CreateEnvironmentBlock(ctypes.byref(env), dup, False):
            env = ctypes.c_void_p()
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = "winsta0\\default"
        pi = PROCESS_INFORMATION()
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        flags = 0x00000400 | 0x08000000
        ok = kernel32.CreateProcessAsUserW(
            dup, None, cmdline, None, None, False, flags,
            env if env.value else None, None, ctypes.byref(si), ctypes.byref(pi),
        )
        if not ok:
            return False
        if pi.hThread:
            kernel32.CloseHandle(pi.hThread)
        if pi.hProcess:
            kernel32.CloseHandle(pi.hProcess)
        return True
    except Exception:
        logger.warning("[operator_alert] interactive launch failed", exc_info=True)
        return False
    finally:
        try:
            if env.value:
                userenv.DestroyEnvironmentBlock(env)
            if dup:
                kernel32.CloseHandle(dup)
            if token:
                kernel32.CloseHandle(token)
        except Exception:
            # Cleanup must not replace the launch result, but a leaked token
            # handle has to be visible when debug logging is on.
            logger.debug("[operator_alert] interactive token cleanup failed", exc_info=True)
