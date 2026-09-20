"""B86/B81 计数与列表同源契约（实施68 P1-12，实施64:181）。

事故：体检报「4 条待审」→ 点「打开待审队列」→ 空（钧 _416/_417）；skuio
「待处理 12 → 空」（_389）。计数为真（reply_diagnosis 按 conversation_id 直查
store），列表假空——面板旧口径是「全平台前 10 条再前端按 chat_key 筛」，平台
积压超过 10 条时本会话的稿子被挤出窗口。B73「去队列放行」补救路径因此断死。

契约（本门禁钉住）：
- ``InboxStore.list_drafts(conversation_id=)`` 精确过滤；
- ``DraftService.list_drafts(conversation_id=)`` 形参存在且与 store 同源——
  体检计数口径（store 按 cid 直查）与列表口径（service 聚合）对同一批数据
  必须逐条一致，**即使平台积压远超单页窗口**；
- ``/api/drafts`` 路由透传 conversation_id（静态契约）。
"""

from __future__ import annotations

import time
from pathlib import Path

from src.inbox.drafts import DraftService
from src.inbox.store import InboxStore


def _seed(store: InboxStore, cid: str, n: int, *, platform: str = "telegram",
          chat_key: str = "", start: float = 0.0) -> None:
    base = start or (time.time() - 3600)
    for i in range(n):
        store.upsert_draft({
            "source_kind": "inbox",
            "source_id": f"{cid}:m{i}",
            "conversation_id": cid,
            "platform": platform,
            "account_id": "acct",
            "chat_key": chat_key or cid.rsplit(":", 1)[-1],
            "peer_text": f"客户消息 {i}",
            "draft_text": f"AI 草稿 {i}",
            "status": "pending",
            "created_at": base + i,
        })


def test_store_list_drafts_conversation_filter(tmp_path: Path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:111", 4)
    _seed(store, "telegram:acct:222", 3)
    rows = store.list_drafts(status="pending", conversation_id="telegram:acct:111")
    assert len(rows) == 4
    assert all(r["conversation_id"] == "telegram:acct:111" for r in rows)


def test_service_count_matches_list_even_under_backlog(tmp_path: Path):
    """核心事故形态：平台积压 30 条、目标会话 4 条。

    旧口径（platform+limit=10）拿到的窗口里可能一条目标会话的稿子都没有——
    计数 4、列表 0。新口径按 conversation_id 过滤后必须逐条对上。
    """
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:111"
    # 干扰积压：同平台 30 条、created_at 更新（占满旧口径的前 10 窗口）
    _seed(store, "telegram:acct:999", 30, start=time.time() - 60)
    # 目标会话 4 条（更老——旧口径必掉出窗口）
    _seed(store, cid, 4, start=time.time() - 7200)

    svc = DraftService(inbox_store=store)

    # 体检计数口径（reply_diagnosis 同款：store 按 cid 直查）
    diag_count = len(store.list_drafts(
        status="pending", conversation_id=cid, limit=20))
    assert diag_count == 4

    # 列表口径（面板新参数）：同一批、逐条一致
    listed = svc.list_drafts(status="pending", conversation_id=cid, limit=20)
    assert len(listed) == diag_count
    assert all(d["conversation_id"] == cid for d in listed)

    # 反面钉住旧病：不带 conversation_id 的前 10 窗口确实看不到目标会话
    # ——这就是「计数真、列表空」的机制本身（防有人回退前端参数时门禁还绿）
    old_window = svc.list_drafts(status="pending", platform="telegram", limit=10)
    assert not any(d["conversation_id"] == cid for d in old_window)


def test_service_conversation_filter_zero_leak(tmp_path: Path):
    """conversation_id 过滤不许把别的会话漏进来（含无 key 行）。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:111", 2)
    _seed(store, "whatsapp:acct:111", 2, platform="whatsapp")  # 同 chat_key 异平台
    svc = DraftService(inbox_store=store)
    listed = svc.list_drafts(
        status="pending", conversation_id="telegram:acct:111", limit=50)
    assert len(listed) == 2
    assert {d["platform"] for d in listed} == {"telegram"}


def test_route_and_panel_static_contract():
    """静态契约：路由透传 conversation_id；面板按 cid 取数；体检直达全局队列。"""
    root = Path(__file__).resolve().parents[1]
    route_src = (root / "src/web/routes/drafts_routes.py").read_text(encoding="utf-8")
    assert 'conversation_id: str = ""' in route_src, \
        "/api/drafts 丢失 conversation_id 形参（B86 回归）"
    tpl = (root / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    assert "conversation_id=${enc(_cid)}" in tpl, \
        "收件箱草稿面板不再按 conversation_id 取数——「计数真、列表空」将复发"
    assert "/workspace/drafts" in tpl.split("function diagOpenDrafts()", 1)[1][:600], \
        "体检「打开待审队列」不再直达全局草稿审批台（B73 放行路径依赖它）"
