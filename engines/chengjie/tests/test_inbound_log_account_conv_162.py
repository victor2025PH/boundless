# -*- coding: utf-8 -*-
"""#162 观测补丁门禁（2026-09-04，I-1-D）。

证据：钧机 16:05-16:28 的日志「收到消息 [私聊/Xh2222222222]」把两个账号收到的消息全记在
同一个用户名下，同一句话 14 秒内出现两次，无法判断是两个号各收一次还是串号。

两条不变量：
  ① TG 入站日志行带 ``account=<id> conv=<cid>``，conv 与 B 线收件箱会话 id 同口径
     （``normalizer.conv_id`` = platform:account:chat_key）；
  ② ``automation_mode_log`` 每行带 ``source``（human/bootstrap/takeover/…），且此前两个
     裸调用（网页聊天默认档 / 协议号陌生人请求预置档）也补了来源。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.inbox.normalizer import conv_id
from src.inbox.store import InboxStore

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_inbound_log_lines_carry_account_and_conv():
    src = _src("src/client/telegram_client.py")
    # 每一处「收到消息 [」日志行都必须带 account= 与 conv=（私聊 + 群组监控两处；
    # f-string 可能跨行续写，取该行起 4 行窗口判）
    all_lines = src.splitlines()
    idx = [i for i, ln in enumerate(all_lines) if "收到消息 [" in ln and "f\"" in ln]
    assert idx, "口径基准日志行未找到（telegram_client 结构变了？）"
    for i in idx:
        window = "\n".join(all_lines[i:i + 4])
        assert "account=" in window and "conv=" in window, window
    # conv 走 normalizer 同口径（B 线会话 id），不是手拼另一套
    assert "from src.inbox.normalizer import conv_id as _mk_cid" in src
    assert '_mk_cid("telegram", _acct, str(message.chat.id))' in src
    assert conv_id("telegram", "acct1", "123") == "telegram:acct1:123"


def test_automation_mode_log_rows_carry_source(tmp_path):
    s = InboxStore(tmp_path / "amlog.db")
    try:
        cid = "telegram:acct1:u1"
        s.set_automation_mode(cid, "auto_ai", source="bootstrap")
        s.set_automation_mode(cid, "manual", source="takeover")
        s.set_automation_mode(cid, "manual", source="takeover")   # 无跃迁不重复落行
        rows = s.list_automation_mode_log(cid, limit=10)
        srcs = [str(r.get("source") or "") for r in rows]
        assert sorted(srcs) == ["bootstrap", "takeover"], rows
        modes = {str(r.get("mode")): str(r.get("source")) for r in rows}
        assert modes == {"auto_ai": "bootstrap", "manual": "takeover"}
        # 旧调用方不传 source → 空串（诚实标未知），不抛
        s.set_automation_mode("telegram:acct1:u2", "review")
        r2 = s.list_automation_mode_log("telegram:acct1:u2", limit=5)
        assert r2 and str(r2[0].get("source") or "") == ""
    finally:
        s.close()


def test_previously_bare_writers_now_pass_source():
    web_chat = _src("src/web/routes/web_chat_routes.py")
    assert 'source="webchat_default"' in web_chat
    acct = _src("src/web/routes/unified_inbox_account_routes.py")
    assert 'source="protocol_request_preset"' in acct
    boot = _src("src/inbox/automation_mode.py")
    assert 'source="bootstrap"' in boot
    # 全站 set_automation_mode 调用：不带 source 的裸调用只允许是 TypeError 回落分支
    # （bulk_set_automation_mode 在 store 侧缺省 source="bulk"，不在此口径内）
    bare = re.compile(r"(?<!\w)set_automation_mode\(\s*[^)]*?\)")
    offenders = []
    for p in (_ROOT / "src").rglob("*.py"):
        if p.name in ("store.py",):
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in bare.finditer(txt):
            call = m.group(0)
            if "source=" in call:
                continue
            # 回落分支：上文 8 行内有 except TypeError
            head = txt[max(0, m.start() - 400):m.start()]
            if "except TypeError" in head:
                continue
            offenders.append(f"{p.relative_to(_ROOT)}: {call[:80]}")
    assert not offenders, "以下写入口没带 source（来源不可审计）：\n  " + "\n  ".join(offenders)
