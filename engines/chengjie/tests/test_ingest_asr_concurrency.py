"""入站语音「落库前转录」的并发闸门禁（2026-09-04 kouxing 事故沉淀）。

事故：协议边车（whatsapp-baileys）冷启动把积压语音**一次性**灌进来（实录 11 秒内
十几路），每路各自 ``await transcribe_voice_message``；转录侧还是**同步阻塞** SDK 调用
且每次新建 ``openai.OpenAI`` 客户端 → 事件循环被按住整个 ASR 时长 + 句柄只涨不落 →
待处理连接越过 Windows ``select()`` 的 512 上限 → uvicorn 整体 ValueError → 后端走
防幽灵 ``exit 78`` 自杀，坐席静默掉线。

这里钉住闸门本身：**无论灌进来多少路，同时在飞的转录不超过配置上限**。
"""

from __future__ import annotations

import asyncio

from src.web.routes import unified_inbox_account_routes as R


class _FakeVtr:
    """记录并发水位的假转录器。"""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    async def transcribe_voice_message(self, path: str, lang: str):
        self.inflight += 1
        self.calls += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            return f"text:{path}:{lang}"
        finally:
            self.inflight -= 1


def _reset_sem() -> None:
    R._INGEST_ASR_SEM = None


async def test_concurrency_capped_by_config():
    _reset_sem()
    vtr = _FakeVtr()
    cfg = {"voice_recognition": {"ingest_concurrency": 2}}
    outs = await asyncio.gather(*[
        R._transcribe_ingest_guarded(vtr, f"/v/{i}.ogg", "auto", cfg)
        for i in range(12)
    ])
    assert vtr.peak <= 2, f"并发水位 {vtr.peak} 越过闸门 2"
    assert vtr.calls == 12, "闸门只排队不丢活：12 条都必须转录"
    assert all(o.startswith("text:") for o in outs)


async def test_default_cap_is_small():
    """缺省档必须是个小数（默认 3）——出厂配置就不该允许无界并发。"""
    _reset_sem()
    vtr = _FakeVtr()
    await asyncio.gather(*[
        R._transcribe_ingest_guarded(vtr, f"/v/{i}.ogg", "zh", None)
        for i in range(10)
    ])
    assert vtr.peak <= R._INGEST_ASR_MAX_CONCURRENCY_DEFAULT
    assert R._INGEST_ASR_MAX_CONCURRENCY_DEFAULT <= 4


async def test_bad_config_falls_back_to_default():
    """脏配置不得变成「无闸」——解析失败一律回落缺省档。"""
    _reset_sem()
    vtr = _FakeVtr()
    cfg = {"voice_recognition": {"ingest_concurrency": "abc"}}
    await asyncio.gather(*[
        R._transcribe_ingest_guarded(vtr, f"/v/{i}.ogg", "zh", cfg)
        for i in range(8)
    ])
    assert vtr.peak <= R._INGEST_ASR_MAX_CONCURRENCY_DEFAULT


async def test_config_clamped_to_sane_band():
    """运营填个 999 也不许把闸门开成事故当天那样。"""
    _reset_sem()
    vtr = _FakeVtr()
    cfg = {"voice_recognition": {"ingest_concurrency": 999}}
    await asyncio.gather(*[
        R._transcribe_ingest_guarded(vtr, f"/v/{i}.ogg", "zh", cfg)
        for i in range(20)
    ])
    assert vtr.peak <= 8, f"并发水位 {vtr.peak} 越过硬上限 8"


def test_ingest_route_goes_through_the_gate():
    """静态接线：入站路径必须过闸，不得再裸调 transcribe_voice_message。

    闸门放在 ``asyncio.wait_for`` **内侧**是刻意的——排队时间算进同一个墙钟预算，
    超时仍按占位符落库（既有退化路径），绝不让排队把边车的 HTTP 拖到重投。
    """
    src = R.__file__.replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    assert "_transcribe_ingest_guarded(" in code
    # wait_for 里包的必须是闸门包装，而不是转录器本身
    assert "_vtr.transcribe_voice_message(str(_vpath)" not in code
