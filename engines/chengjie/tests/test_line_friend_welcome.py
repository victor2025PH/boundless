# -*- coding: utf-8 -*-
"""LINE 好友欢迎 + 接受页 XML 解析单测（Phase16）。"""
from src.integrations.line_rpa import ui_hierarchy as ui
from src.integrations.line_rpa.friend_welcome import (
    build_welcome_text,
    enqueue_friend_welcome,
    parse_welcome_cfg,
)


def _friend_req_xml(name: str, accept_text: str = "Accept") -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" />
  <node class="android.widget.TextView" text="{name}"
        bounds="[60,400][520,460]" />
  <node class="android.widget.Button" text="{accept_text}"
        bounds="[820,390][980,470]" />
  <node class="android.widget.TextView" text="Bob"
        bounds="[60,560][520,620]" />
  <node class="android.widget.Button" text="{accept_text}"
        bounds="[820,550][980,630]" />
</hierarchy>""".encode("utf-8")


def test_find_friend_accept_rows_pairs_name_with_button():
    rows = ui.find_friend_accept_rows(_friend_req_xml("Alice"))
    assert len(rows) == 2
    assert rows[0].name == "Alice"
    assert rows[1].name == "Bob"
    assert rows[0].accept_y < rows[1].accept_y


def test_build_welcome_text_companion_tone():
    text = build_welcome_text(lang="zh", welcome_cfg={"enabled": True})
    assert "客服" not in text
    assert "嗨" in text or "加上" in text


def test_enqueue_friend_welcome_dedup(tmp_path):
    from src.integrations.line_rpa.state_store import LineRpaStateStore

    db = LineRpaStateStore(tmp_path / "line.db")
    q1 = enqueue_friend_welcome(db, peer_name="Alice", text="hi")
    q2 = enqueue_friend_welcome(db, peer_name="Alice", text="hi again")
    assert q1 > 0
    assert q2 == 0


def test_parse_welcome_cfg_defaults():
    cfg = parse_welcome_cfg({"welcome": {"enabled": True}})
    assert cfg["enabled"] is True
    assert cfg["scene"] == "companion_line_welcome"
