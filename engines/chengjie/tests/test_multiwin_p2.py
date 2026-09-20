"""P2 多开治理门禁（2026-07-29）：人设乐观锁 + 坐席窗口观测。

锁定三条不变量：
  1. ``profile_rev``——内容指纹纯函数：同内容恒同、键序无关、任一字段变化即变、空盘 ""。
  2. 人设保存乐观锁——PUT 带 expected_rev：指纹过期/人设被删 → 409（表单内容不丢，
     前端确认后免键重发＝显式覆盖）；不带 expected_rev＝旧契约零破坏；GET/PUT 均回 rev。
  3. AgentCoordinator 窗口注册表——heartbeat 携窗口指纹 → 按坐席聚合 {窗口数, 待机数}，
     TTL 过期自清（关窗/崩溃不残留）；presence 名单并出 windows 字段。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.persona_manager import PersonaManager, profile_rev
from src.workspace.agent_coordinator import AgentCoordinator

_HDRS = {"Authorization": "Bearer test-token",
         "Content-Type": "application/json"}


# ── 1. profile_rev 纯函数 ──────────────────────────────────────


def test_profile_rev_deterministic_and_order_insensitive():
    a = {"name": "苏婉", "age": 32, "tags": ["a", "b"]}
    b = {"tags": ["a", "b"], "age": 32, "name": "苏婉"}   # 键序不同
    assert profile_rev(a) == profile_rev(b)
    assert len(profile_rev(a)) == 12

    changed = dict(a, age=33)
    assert profile_rev(changed) != profile_rev(a)

    nested = {"personality": {"style": "干练", "traits": ["爽利"]}}
    nested2 = {"personality": {"traits": ["爽利"], "style": "干练"}}
    assert profile_rev(nested) == profile_rev(nested2)

    # None（文档不存在）→ ""；{}（存在但为空）→ 真指纹。
    # 若空文档也返回 ""，前端不会带 expected_rev → 「两窗口都从空表开始各加一批」
    # 这种最典型的丢更新场景毫无保护（意图关键词表实测踩到，2026-07-29 修）。
    assert profile_rev(None) == ""
    assert profile_rev({}) != ""
    assert len(profile_rev({})) == 12
    assert profile_rev({}) != profile_rev({"a": 1})


# ── 2. 人设保存乐观锁（路由级） ─────────────────────────────────


@pytest.fixture
def persona_app(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
        ]},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    PersonaManager.get_instance().load_profiles_from_config(
        {"personas": cfg["personas"]})

    from src.web.admin import create_app
    app = create_app(cm)
    yield app
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_profile_put_stale_rev_409_and_matching_rev_ok(persona_app):
    async with _client(persona_app) as c:
        r = await c.get("/api/personas/profiles/chen_mo", headers=_HDRS)
        assert r.status_code == 200
        rev = r.json().get("rev")
        assert rev

        # 窗口 A 用正确 rev 保存 → 通过，响应带新 rev
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS,
                        json={"persona": {"name": "陈默", "role": "陪伴", "age": 30},
                              "merge": True, "expected_rev": rev})
        assert r.status_code == 200
        new_rev = r.json().get("rev")
        assert new_rev and new_rev != rev

        # 窗口 B 还拿着旧 rev 保存 → 409（保护 A 刚写入的改动）
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS,
                        json={"persona": {"name": "陈默B"},
                              "merge": True, "expected_rev": rev})
        assert r.status_code == 409

        # B 显式确认覆盖：免键重发 → 旧契约放行（last-write 由人确认）
        r = await c.put("/api/personas/profiles/chen_mo", headers=_HDRS,
                        json={"persona": {"name": "陈默B"}, "merge": True})
        assert r.status_code == 200

        # 对已删除人设带 rev 保存 → 409（不静默复活）
        r = await c.put("/api/personas/profiles/ghost_x", headers=_HDRS,
                        json={"persona": {"name": "幽灵"},
                              "expected_rev": "deadbeef0000"})
        assert r.status_code == 409


@pytest.fixture
def isolated_global_rules(tmp_path):
    """播种 tmp 版 global_rules，并**守住仓库真实文件一字未动**。

    ⚠️ 2026-07-29 实锤事故（本测试首版造成）：``PersonaManager.save_global_rules``
    的落盘路径由 ``Path(__file__).parents[2]/config/global_rules.yaml`` 推导——
    **无视 tmp_path**，直接写**仓库**里那份生产在用的文件（引擎按 mtime 热加载）。
    首版路由测试因此把 13 条回复硬约束清成 ``[]``，并经备份轮转把测试数据推进
    ``.bak.1`` 槽位（运维点「恢复槽位1」会二次清空）。事后从 git HEAD 还原。

    重定向本身现由 conftest 的 autouse ``_isolated_global_rules`` 全局兜底（所有
    测试都受保护，不靠各文件自觉）。本 fixture 只做两件补充：① 播种一份**真实
    schema** 的内容，让路由测试有初始态可改；② 断言仓库文件字节/内容未变——
    这是对 conftest 那层重定向的**自我校验**，谁把它改坏了这里立刻红。
    """
    import hashlib

    from src.utils.persona_manager import GLOBAL_RULES_FILENAME
    repo_file = (Path(__file__).resolve().parent.parent / "config"
                 / GLOBAL_RULES_FILENAME)
    before = None
    if repo_file.exists():
        raw = repo_file.read_bytes()
        before = (len(raw), hashlib.sha1(raw).hexdigest())

    tmp_rules = tmp_path / GLOBAL_RULES_FILENAME
    # 用**真实 schema**（{id,enabled,title,rule}）播种，别用臆造字段
    tmp_rules.write_text(yaml.dump({
        "reply_constraints": [
            {"id": "seed", "enabled": True, "title": "种子约束",
             "rule": "测试用，不进生产"},
        ],
    }, allow_unicode=True), encoding="utf-8")

    pm = PersonaManager.get_instance()
    pm._global_rules_path = tmp_rules      # noqa: SLF001 — 正是要拦的那个字段
    pm._global_rules = None
    pm._global_rules_sig = ("", 0.0, -1)   # 三元组：(路径, mtime, size)
    yield tmp_rules

    if before is not None:
        raw2 = repo_file.read_bytes()
        assert (len(raw2), hashlib.sha1(raw2).hexdigest()) == before, (
            "测试写到了仓库真实 global_rules.yaml（生产在用、按 mtime 热加载）——"
            "conftest._isolated_global_rules 重定向失效，必须修复后再跑")


@pytest.mark.asyncio
async def test_global_rules_optimistic_lock(persona_app, isolated_global_rules):
    """全局规则（影响所有人设，覆盖破坏面最大）同款乐观锁：陈旧 rev → 409。"""
    async with _client(persona_app) as c:
        r = await c.get("/api/persona/global-rules", headers=_HDRS)
        assert r.status_code == 200
        rev = r.json().get("rev")
        assert rev, "GET 必须回 rev（前端据此带 expected_rev）"

        # 真实 schema：{id, enabled, title, rule}（首版误用臆造的 {text,...}，
        # _assemble_constraints 会渲染成空行——测试数据也要照真结构写）
        rules = {"reply_constraints": [
            {"id": "win_a", "enabled": True, "title": "窗口A改的",
             "rule": "窗口A写入的约束"},
        ]}
        r = await c.put("/api/persona/global-rules", headers=_HDRS,
                        json={"rules": rules, "expected_rev": rev})
        assert r.status_code == 200
        new_rev = r.json().get("rev")
        assert new_rev and new_rev != rev

        # 另一窗口拿旧 rev 保存 → 409（保护刚写入的改动）
        stale_payload = {"reply_constraints": [
            {"id": "win_b", "enabled": True, "title": "窗口B改的",
             "rule": "窗口B写入的约束"},
        ]}
        r = await c.put("/api/persona/global-rules", headers=_HDRS,
                        json={"rules": stale_payload, "expected_rev": rev})
        assert r.status_code == 409
        # 409 必须是**拒写**：磁盘内容仍是窗口A的（不是被B悄悄盖掉）
        on_disk = yaml.safe_load(
            isolated_global_rules.read_text(encoding="utf-8")) or {}
        assert on_disk["reply_constraints"][0]["id"] == "win_a"

        # 显式确认覆盖（不带 rev）→ 旧契约放行
        r = await c.put("/api/persona/global-rules", headers=_HDRS,
                        json={"rules": stale_payload})
        assert r.status_code == 200
        on_disk = yaml.safe_load(
            isolated_global_rules.read_text(encoding="utf-8")) or {}
        assert on_disk["reply_constraints"][0]["id"] == "win_b"


@pytest.mark.asyncio
async def test_profile_put_without_rev_keeps_old_contract(persona_app):
    """老客户端/批量工具/导入不带 expected_rev → 整条旧契约不变。"""
    async with _client(persona_app) as c:
        r = await c.put("/api/personas/profiles/new_one", headers=_HDRS,
                        json={"persona": {"name": "新人", "role": "x"}})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body.get("rev")

        r = await c.get("/api/personas/profiles/new_one", headers=_HDRS)
        assert r.status_code == 200
        assert r.json().get("rev") == body["rev"]


# ── 3. 坐席窗口注册表 ──────────────────────────────────────────


def test_coordinator_window_registry_counts_and_ttl(monkeypatch):
    coord = AgentCoordinator(store=None)
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])

    coord.record_window("alice", "w1", path="/workspace", standby=False)
    coord.record_window("alice", "w2", path="/workspace", standby=True)
    coord.record_window("bob", "w3", path="/workspace/dashboard", standby=False)
    snap = coord.windows_snapshot()
    assert snap["alice"]["windows"] == 2
    assert snap["alice"]["standby"] == 1
    assert snap["bob"]["windows"] == 1
    assert "/workspace" in snap["alice"]["paths"]

    # 同窗口重复心跳＝续租不加数；待机态翻转以最新为准
    coord.record_window("alice", "w2", path="/workspace", standby=False)
    snap = coord.windows_snapshot()
    assert snap["alice"]["windows"] == 2
    assert snap["alice"]["standby"] == 0

    # TTL 过期自清（关窗/崩溃后不残留）
    t[0] += AgentCoordinator.WINDOW_TTL_SEC + 1
    coord.record_window("bob", "w3", path="/workspace/dashboard")   # bob 还活着
    snap = coord.windows_snapshot()
    assert "alice" not in snap
    assert snap["bob"]["windows"] == 1

    # 空/非法入参不炸不记
    coord.record_window("", "wx")
    coord.record_window("carol", "")
    assert "carol" not in coord.windows_snapshot()
