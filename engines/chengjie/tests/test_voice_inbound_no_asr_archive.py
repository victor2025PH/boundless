"""入站语音「识别不可用」分支的归档接线钉（2026-08-20 内测实录）。

事故：客户包 voice_recognition 关闭 → 入站语音只落「[语音消息 - 识别功能未启用]」
占位文字、**不归档音频**——坐席看得到占位却听不了原音，用户体感＝「新版本语音
消息无法接收」。修复＝该分支同样下载并经 ``publish_outbound_media`` 归档
（与转录分支同管道），最坏也能人工听。

行为级测试需要重演整个 pyrogram 消息处理器（重 mock 价值低），这里按 house
wiring-pin 风格钉源码结构：占位分支内必须存在归档调用，防止后续重构把它
剥回「只落占位」的旧行为。
"""
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "src" / "client" / "telegram_client.py").read_text(encoding="utf-8")


def test_no_asr_branch_still_archives_audio():
    # 锚定「识别功能未启用」占位分支段落（占位赋值后、图片处理段之前）
    anchor = _SRC.index("[语音消息 - 识别功能未启用]")
    tail = _SRC[anchor:anchor + 1800]
    assert "publish_outbound_media" in tail, (
        "识别不可用分支丢了音频归档——坐席将只见占位文字听不到原音"
        "（=用户报的「语音消息无法接收」回归）")
    assert "voice_media_ref" in tail, "归档 URL 必须回填 voice_media_ref（媒体行可回放）"


def test_transcribe_branch_archives_before_transcription():
    # 转录分支的既有不变量：先归档后转录（转录失败仍有原音可听）
    dl = _SRC.index("入站语音留档（P1-2")
    tr = _SRC.index("调用转录服务")
    assert dl < tr
