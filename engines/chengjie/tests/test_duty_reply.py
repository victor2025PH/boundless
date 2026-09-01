# -*- coding: utf-8 -*-
"""值守群回复 CLI（tools/duty_reply.py）纯函数门禁（实施81 P0-1）。

守住的决策：
- 群别名解析大小写不敏感、原始 id 直通、空值必抛（打错群=对无关群实弹）；
- client_msg_id 自动唯一（旧 tmp 脚本固定 id 重跑被 send_dedup 静默吞）；
- send 载荷与值守 SOP 同参（skip_translate/force_lang/幂等键；reply_to 可选）；
- 台账行字段契约（值守交接消费）。
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_reply import (  # noqa: E402
    GROUP_ALIASES,
    build_client_msg_id,
    build_ledger_row,
    build_send_payload,
    resolve_group,
)


def test_resolve_group_aliases_and_passthrough():
    assert resolve_group("neice") == "-1004345824259"
    assert resolve_group("NEICE") == "-1004345824259"
    assert resolve_group("官方") == "-1004290740529"
    assert resolve_group("-100999") == "-100999"
    with pytest.raises(ValueError):
        resolve_group("  ")
    # 两个别名指向两个不同群（复制粘贴错误的回归钉）
    assert GROUP_ALIASES["neice"] != GROUP_ALIASES["official"]


def test_client_msg_id_unique_and_shaped():
    a = build_client_msg_id(now=1756392000.0, rand="abcd")
    assert a.startswith("duty-") and a.endswith("-abcd")
    # 同秒不同随机段 → 不同 id（自动唯一是本函数存在的理由）
    b = build_client_msg_id(now=1756392000.0)
    c = build_client_msg_id(now=1756392000.0)
    assert b != c


def test_send_payload_contract():
    p = build_send_payload("-1004345824259", "收到", account_id="6834964252",
                           client_msg_id="duty-x")
    assert p["platform"] == "telegram"
    assert p["skip_translate"] is True and p["force_lang"] == 1
    assert p["client_msg_id"] == "duty-x"
    assert "reply_to" not in p
    p2 = build_send_payload("-1", "t", account_id="a",
                            client_msg_id="i", reply_to_id=77)
    assert p2["reply_to"] == {"id": 77}


def test_ledger_row_contract():
    import re
    import time
    row = build_ledger_row(chat_key="-100", text="x" * 999, ticket=12,
                           path="bug-intake/reply", client_msg_id="duty-1",
                           ok=True, now=time.time())
    assert row["ticket"] == 12 and row["ok"] is True
    assert row["path"] == "bug-intake/reply"
    assert len(row["text"]) == 500          # 台账截断，防超长灌爆
    # when＝人读格式（锚形状不锚日期——夹具时间炸弹教训）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["when"])


# ── 对外答复红线（2026-08-29 老板指令：不得教用户改配置文件）────────────


def test_config_edit_guard_blocks_incident_text():
    # 正例＝当日群实录逐段（引发老板指令的那条「方法二」），四类信号全命中
    from src.ops.support_reply_guard import config_edit_hits
    incident = (
        "【办法二】调大总上限（要改一次配置文件）："
        "打开智聊数据目录的 config\\config.local.yaml，"
        "找到 companion_send_gate，在它的下面加一行（前面敲两个空格）："
        "target_cap: 500（数字你定），保存后重启客户端生效。"
    )
    hits = config_edit_hits(incident)
    kinds = {h.split(":", 1)[0] for h in hits}
    assert {"file", "key", "verb"} <= kinds
    # 文件名/内部键名各自单独出现也必须拦（教程常被拆成多条消息发）
    assert config_edit_hits("打开 config.local.yaml 加一行")
    assert config_edit_hits("在 companion_send_gate 下面改 target_cap")
    assert config_edit_hits("用记事本打开 config 目录里的那个文件")
    assert config_edit_hits("加一行 target_cap: 500")
    assert config_edit_hits("编辑 profiles_runtime.yaml 里的音色")
    # YAML 蛇形键值行（未收录进键名表的新键也拦得住）
    assert any(h.startswith("yaml_kv:")
               for h in config_edit_hits("下面加：\nsome_new_flag: true"))


def test_config_edit_guard_passes_normal_replies():
    # 反例＝值守日常话术与「指向后台设置页」的正确答法，零误拦
    from src.ops.support_reply_guard import config_edit_hits
    ok_texts = [
        "@skuio 收到，正在查",
        "该问题已修复，更新到 1.0.59 版本即可",
        "白名单按钮在聊天框上方的黄色横幅里，点「白名单此客户」即可",
        "额度设置已收进后台「自动回复设置」页，下版本随包上线",
        "这个额度是随安装包的默认值 300/天，防轰炸保护用的",
        "备注: 我们今晚发布修复",          # 中文冒号句不是 YAML
        "note: fix will ship tonight",      # 无下划线蛇形键不误判
        "",
    ]
    for t in ok_texts:
        assert config_edit_hits(t) == [], t


def test_config_edit_guard_wired_into_cli():
    # CLI 接线钉：main() 里必须在发送前调 config_edit_hits（防将来重构掉线）
    import inspect
    import tools.duty_reply as dr
    src = inspect.getsource(dr.main)
    assert "config_edit_hits" in src
    assert src.index("config_edit_hits") < src.index("dry_run")
