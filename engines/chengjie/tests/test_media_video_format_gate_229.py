# -*- coding: utf-8 -*-
"""#229 #231（M-3 B，D-M4，2026-09-06）：视频容器口径——选文件时即判，白名单与后端同源。

DR2QWV / GAJ5T2：5MB IMG_7312.MOV 在 LINE / WA / Messenger 三处「结果未知」。真根因是 2MB body
闸（见 test_media_send_stream_m3），**不是**格式——前端此前根本没有格式白名单；但 D-M4 的决策
仍要落：.MOV（iPhone 默认）有 ffmpeg 就自动换封装 MP4，没有就在选文件时当场拒绝并写明，
其它视频容器一律 MP4/WEBM 白名单，不再让任何视频进队列去复用「结果未知」。

守：① 前端判定读 send-caps.video_exts / video_transcode_exts（老后端无键＝不判，模板热更新
先于后端重启的中间态自洽）；② 拒绝落 console.warn 且带 error 字样（zl_collect 过滤词）；
③ 入队件标「将自动转为 MP4」；④ 后端表：native ∩ transcode = ∅，两者 ⊂ _OUT_VIDEO_EXT。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def _fn(src: str, name: str) -> str:
    i = src.find(name)
    assert i > 0, f"{name} 失踪"
    return src[i:src.find("\n}\n", i)]


def test_verdict_reads_same_source_caps_and_tolerates_old_backend():
    src = _TPL.read_text(encoding="utf-8")
    v = _fn(src, "function _videoFormatVerdict(file)")
    assert "if(!Array.isArray(c.video_exts)) return {ok:true" in v, "老后端无键必须放行"
    assert "c.video_exts.indexOf(ext)>=0" in v
    assert "c.video_transcode_exts" in v and "transcode:true" in v
    assert "return {ok:false" in v


def test_stage_rejects_at_selection_with_log_and_marks_transcode():
    src = _TPL.read_text(encoding="utf-8")
    stage = _fn(src, "function _stageMedia(file)")
    i_fmt, i_cap, i_push = (stage.find("_videoFormatVerdict(file)"), stage.find("_mediaCapMb(file)"),
                            stage.find("_mediaQueue.push("))
    assert 0 < i_fmt < i_cap < i_push, "格式判定必须在上限判定与入队之前"
    assert "inbox.media.video_fmt_reject" in stage
    assert "console.warn('[media] rejected error=unsupported_format" in stage
    assert "transcode:!!vf.transcode" in stage
    item = _fn(src, "function _mediaItemHtml(it)")
    assert "inbox.media.will_transcode" in item


def test_i18n_keys_zh_en():
    pack = _I18N.read_text(encoding="utf-8")
    for k in ("inbox.media.video_fmt_reject", "inbox.media.will_transcode",
              "inbox.media.will_transcode_toast"):
        assert pack.count(f'"{k}"') == 2, k
    zh = next(ln for ln in pack.splitlines() if ln.strip().startswith('"inbox.media.video_fmt_reject"'))
    assert "{ext}" in zh and "{ok}" in zh
