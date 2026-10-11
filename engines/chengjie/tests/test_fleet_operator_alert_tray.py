# -*- coding: utf-8 -*-
"""Room to-do panel: minimize hides to tray, explicit hide-to-tray button, tray double-click restores, new alerts re-show."""
from src.fleet import operator_alert as oa


def test_panel_script_ascii_and_tray_behaviour():
    s = oa.PANEL_SCRIPT
    assert s.isascii()
    assert "$form.MinimizeBox = $true" in s
    assert "$trayBtn.add_Click({ $form.Hide() })" in s
    assert "FormWindowState]::Minimized) { $form.Hide() }" in s
    assert "$notify.add_DoubleClick({" in s
    assert "$raised.Length -gt 0" in s and "$form.Show()" in s
    assert "Microsoft YaHei UI" in s and "$btn.Text = Txt $snap $script:lang 'toggle'" in s


def test_tray_button_strings_both_languages():
    zh = oa.STRINGS["zh"] if hasattr(oa, "STRINGS") else None
    packs = [v for v in vars(oa).values() if isinstance(v, dict) and "zh" in v and "en" in v and isinstance(v["zh"], dict)]
    pack = zh or packs[0]
    assert pack["tray_btn"] == "收到托盘" if zh else packs[0]["zh"]["tray_btn"] == "收到托盘"
    assert packs[0]["en"]["tray_btn"] == "Hide to tray"
