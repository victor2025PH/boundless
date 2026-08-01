"""companion 号发媒体能力门禁（2026-07-31 补的缺陷）。

**缺陷**：开着 ``companion_runtime`` 的部署里，``TelegramCompanionWorker`` 从没实现
``send_media``（类 docstring 却一直写着「可选 send_media(...)」＝本就是设计意图），
于是 ``owns_media("telegram", *)`` 恒 False。生产接口自己报出来的后果：

    telegram  can_media=False can_voice=False voice_mode=none
    whatsapp  can_media=True  can_voice=True  voice_mode=composer
    line      can_media=True  can_voice=True  voice_mode=composer

即**流量最大的 Telegram 是唯一一个坐席不能手动发图/发语音的平台**（按钮置灰、
硬点 501），而 AI 反而能发（A 线原生路径绕开编排器）。

守三件事：
1. 能力**按开关绑定**——``owns_media`` 判据是 hasattr，写成普通方法＝把三条消费链
   一次性全开（其中「主动触达语音」属运营决策）。
2. 走**内层 pyrogram**，不委派 A 线 ``send_photo``——后者自带镜像，编排器也镜像，
   委派过去一次发送会在坐席台出现两行。
3. 四种媒体类型都路由到对的 pyrogram 方法。
"""

import pytest

from src.integrations.account_orchestrator import AccountOrchestrator, account_key
from src.integrations.telegram_companion_worker import (
    TelegramCompanionWorker, companion_media_enabled,
)


def _cfg(**tg):
    return {"platform_login": {"telegram": dict(tg)}} if tg else {}


def _worker(**tg):
    return TelegramCompanionWorker({"account_id": "tg1", "meta": {}}, _cfg(**tg))


class _FakeInner:
    """鸭子类型的内层 pyrogram client：记录调用。"""

    def __init__(self):
        self.calls = []

    async def _rec(self, name, target, path, caption):
        self.calls.append((name, target, path, caption))
        return type("M", (), {"id": 4242})()

    async def send_photo(self, t, p, caption=""):
        return await self._rec("photo", t, p, caption)

    async def send_voice(self, t, p, caption=""):
        return await self._rec("voice", t, p, caption)

    async def send_video(self, t, p, caption=""):
        return await self._rec("video", t, p, caption)

    async def send_document(self, t, p, caption=""):
        return await self._rec("document", t, p, caption)


class _FakeAline:
    """A 线 TelegramClient 壳：内层 pyrogram 在 .client 上（与真实结构一致）。"""

    def __init__(self, inner):
        self.client = inner
        self.sent_photos = []

    async def send_photo(self, chat_id, path, caption=""):
        # 真实实现会**自带收件箱镜像**——本测试用它来证明我们没有走这条路
        self.sent_photos.append((chat_id, path, caption))
        return True


# ─────────────────── 开关 ───────────────────

def test_flag_defaults_off():
    assert companion_media_enabled({}) is False
    assert companion_media_enabled(_cfg(companion_runtime=True)) is False
    assert companion_media_enabled(_cfg(companion_media=True)) is True


def test_send_media_attribute_gated_by_switch():
    """``owns_media()`` 判据就是 ``hasattr(worker,"send_media")``。

    谁把 ``_send_media_impl`` 改成普通 ``send_media``，这条就红——那等于一次性放开
    坐席手动发送 + B 线自动发媒体 + 主动触达语音三条链，最后一条属运营决策。
    """
    assert not hasattr(_worker(), "send_media")
    assert not hasattr(_worker(companion_media=False), "send_media")
    assert hasattr(_worker(companion_media=True), "send_media")


def test_owns_media_reflects_switch():
    """端到端过真编排器判定（而不是只测 hasattr，防判据将来改了这里还绿）。"""
    for flag, expect in ((False, False), (True, True)):
        orch = AccountOrchestrator(config=_cfg(companion_media=flag))
        w = _worker(companion_media=flag)
        orch._managed[account_key("telegram", "tg1")] = type(
            "M", (), {"state": "running", "worker": w})()
        assert orch.owns_media("telegram", "tg1") is expect


# ─────────────────── 发送行为 ───────────────────

@pytest.mark.parametrize("mtype,expect", [
    ("image", "photo"), ("voice", "voice"), ("video", "video"),
    ("document", "document"), ("", "document"), ("weird", "document"),
])
async def test_media_type_routes_to_right_pyrogram_call(mtype, expect):
    w = _worker(companion_media=True)
    inner = _FakeInner()
    w.client = _FakeAline(inner)
    res = await w.send_media("123", media_path="/tmp/x.bin", media_type=mtype,
                             caption="hi")
    assert res == {"delivered": True, "message_id": "4242"}
    assert inner.calls[0][0] == expect
    assert inner.calls[0][3] == "hi"


async def test_does_not_delegate_to_mirroring_wrapper():
    """核心不变量：走内层 pyrogram，**不**调 A 线 ``send_photo``。

    A 线那个方法自带收件箱出站镜像，而 ``AccountOrchestrator.send_media`` 发完也会
    镜像一次——委派过去，一次发送会在坐席台留下两行。
    """
    w = _worker(companion_media=True)
    inner = _FakeInner()
    aline = _FakeAline(inner)
    w.client = aline
    await w.send_media("123", media_path="/tmp/a.jpg", media_type="image")
    assert inner.calls and inner.calls[0][0] == "photo"
    assert aline.sent_photos == [], "不该走会二次镜像的 A 线 send_photo"


async def test_chat_key_numeric_coercion():
    """chat_key 是数字串就转 int（pyrogram 对 peer 类型敏感），非数字原样传。"""
    w = _worker(companion_media=True)
    inner = _FakeInner()
    w.client = _FakeAline(inner)
    await w.send_media("-1001234", media_path="/tmp/a.jpg", media_type="image")
    await w.send_media("@somechannel", media_path="/tmp/a.jpg", media_type="image")
    assert inner.calls[0][1] == -1001234
    assert inner.calls[1][1] == "@somechannel"


async def test_raises_when_client_absent():
    """没连上就如实抛（编排器据此判失败），别静默返回 delivered=True。"""
    w = _worker(companion_media=True)
    w.client = None
    with pytest.raises(RuntimeError):
        await w.send_media("1", media_path="/tmp/a.jpg", media_type="image")
    w.client = _FakeAline(None)   # 壳在、内层没起来
    with pytest.raises(RuntimeError):
        await w.send_media("1", media_path="/tmp/a.jpg", media_type="image")


# ─────────────────── 与能力矩阵联动 ───────────────────

def test_matrix_marks_the_cell_as_switched():
    """矩阵应把这一格标成「开关」并点名开关路径（而不是显示成不支持）。"""
    from src.integrations import platform_capabilities as PC
    key = "telegram:protocol(companion)"
    row = PC.capability_matrix({}).get(key)
    if row is None or not row["available"]:
        pytest.skip("本机构造不出 companion worker")
    assert row["caps"]["send_media"] is False          # 出厂关
    assert PC.switched_cells({}).get(key, {}).get("send_media") == \
        "platform_login.telegram.companion_media"
