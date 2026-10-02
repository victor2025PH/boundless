"""P2-6：安装器 / 本机页里的旧名字（ChatX Fleet、舰队节点、舰队控制台）统一成「智拓群控」。

只改显示名：计划任务名、安装目录、AppId 不变，否则已装的电脑升级会装成第二份。
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
ISS = ENGINE / "fleet_agent/setup/ChatXAgent.iss"


def test_iss_display_names_are_zhituo_and_ids_unchanged():
    iss = ISS.read_text(encoding="utf-8-sig")
    assert '#define AppName "智拓群控节点"' in iss and "DefaultGroupName=智拓群控" in iss
    assert "AppId={{A7C3E1B2-4F58-4C0A-9B1E-6D2F8A0C4E71}" in iss
    assert "DefaultDirName={autopf}\\ChatX Agent" in iss
    assert '/End /TN "ChatX Fleet Agent"' in iss            # 计划任务名不改
    icons = iss.split("[Icons]")[1].split("[Code]")[0]
    assert "智拓群控节点" in icons and "智拓群控控制台" in icons and "舰队" not in icons
    # 舰队 只允许出现在清理旧快捷方式的 [InstallDelete] 里
    rest = iss.split("[InstallDelete]")[0] + iss.split("[InstallDelete]")[1].split("[Icons]")[1]
    assert "舰队" not in rest and "ChatX Fleet\n" not in rest
    old = iss.split("[InstallDelete]")[1].split("[Icons]")[0]
    for name in ("{autodesktop}\\舰队节点.lnk", "{autoprograms}\\舰队节点.lnk", "{autoprograms}\\舰队控制台.lnk"):
        assert name in old


def test_panel_and_cli_use_new_name():
    from src.fleet.service import TASK_NAME
    assert TASK_NAME == "ChatX Fleet Agent"
    for rel in ("src/fleet/panel.py", "src/fleet/agent.py", "src/fleet/local_status.py"):
        assert "舰队" not in (ENGINE / rel).read_text(encoding="utf-8"), rel
    panel = (ENGINE / "src/fleet/panel.py").read_text(encoding="utf-8")
    assert "<title>智拓群控节点</title>" in panel and "打开智拓群控控制台" in panel


def test_home_and_docs_keep_old_name_as_hint_only():
    home = (ENGINE / "domains/fleet_control/web/templates/fleet_home.html").read_text(encoding="utf-8")
    assert "智拓群控节点" in home
    assert re.findall("舰队[^」]*」", home) == ["舰队节点」"] and "旧版叫「舰队节点」" in home


def test_room_pack_readme_named_and_bom():
    from src.fleet.roompack import _readme
    txt = _readme("机房A", "", None, 5)
    assert txt.startswith("\ufeff智拓群控 机房安装包") and "ChatX fleet" not in txt

def test_cli_help_uses_new_brand():
    for rel in ("src/fleet/agent.py", "src/fleet/admin.py"):
        txt = (ENGINE / rel).read_text(encoding="utf-8")
        assert "智拓群控" in txt and 'description="智控' not in txt
