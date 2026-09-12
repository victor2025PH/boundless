# -*- coding: utf-8 -*-
"""Q-35 #316（4HK54G）A 段静态门禁：相册批量上传**逐文件**结果可见。

事故形状：`pmaUpload()` 逐文件串行 POST，失败只 `fail++`（不记文件名 / HTTP 状态 / 响应体），
结尾一句「成功 N / 失败 M」还会被紧接着的 `pmaLoad()` 整页重绘抹掉——报障时「哪几张、为什么」
前后端都答不上。本门禁钉住模板侧契约（模板热更新直上生产，没有别的护栏）：

- 逐文件记录字段 `{name, size, ok, existed, status, reason, detail}`；
- 409 / `deduped` ＝「已存在」不算失败；网络异常 `network:<msg>`、超时 `timeout`；
- 结果容器 `#pma-up-result` 在 uploader 里、`_pmaRender` 重绘后由 `_pmaRenderUploadResult` 回填；
- 「重试失败项」只重发失败的 File；内联 onclick 用到的两个函数挂 window；
- 原因人话全部走 i18n（`pma_fr_*` / `pma_up_*` zh + en 齐平）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"


def _src() -> str:
    return _TPL.read_text(encoding="utf-8", errors="replace")


def test_per_file_record_shape_and_dedup_semantics():
    s = _src()
    body = s[s.index("async function pmaUpload("):s.index("async function pmaDeleteItem(")]
    for field in ("name:", "size:", "file:", "ok:", "existed:", "status:", "reason:", "detail:"):
        assert field in body, f"逐文件记录缺字段 {field}"
    assert "r.status === 409" in body and "existed++" in body, "409 必须算「已存在」不算失败"
    assert "d.deduped" in body, "200+deduped 必须算「已存在」"
    assert "'timeout'" in body and "'network:'" in body, "网络异常 / 超时要分开记 reason"
    assert "d.reason || d.error" in body, "reason 优先读服务端机器码（B 段 {ok:false, reason, detail}）"
    assert "_pmaUpLast = {" in body and "results: results" in body, "结果必须持久到 _pmaUpLast 供重绘 / 重试"
    # 结果条在 pmaLoad 整页重绘之后仍在：渲染器挂在 _pmaRender 尾部
    render = s[s.index("function _pmaRender("):s.index("function _pmaRenderGrid(")]
    assert 'id="pma-up-result"' in render, "uploader 里缺 #pma-up-result 容器"
    assert "_pmaRenderUploadResult()" in render, "_pmaRender 重绘后必须回填结果条（否则被 pmaLoad 抹掉＝旧病）"


def test_retry_only_reposts_failed_files_and_handlers_exposed():
    s = _src()
    retry = s[s.index("async function _pmaUpRetryFails("):s.index("window._pmaUpToggleFails")]
    assert "!x.ok && x.file" in retry, "重试必须只挑失败且仍持有 File 的项"
    assert "await pmaUpload(files)" in retry
    assert "window._pmaUpToggleFails = _pmaUpToggleFails;" in s
    assert "window._pmaUpRetryFails = _pmaUpRetryFails;" in s
    # 内联 onclick 的两个入口确实被渲染器引用
    rend = s[s.index("function _pmaRenderUploadResult("):s.index("function _pmaUpToggleFails(")]
    assert 'onclick="_pmaUpToggleFails()"' in rend and 'onclick="_pmaUpRetryFails()"' in rend
    # 文件名 / detail 一律转义后进 innerHTML
    assert "_pmaEsc(x.name" in rend and "_pmaEsc(_pmaFailReasonText(x))" in rend


def test_reason_texts_cover_server_reasons_and_are_bilingual():
    import importlib
    pack = importlib.import_module("src.web.i18n_packs.persona_apply_modal")
    zh, en = pack.ZH, pack.EN
    s = _src()
    fn = s[s.index("function _pmaFailReasonText("):s.index("function _pmaRenderUploadResult(")]
    # 服务端 B 段的机器码每个都要有人话分支
    for reason in ("too_large", "ext_not_allowed", "bad_content", "too_long", "empty_file",
                   "timeout", "network", "readonly"):
        assert f"'{reason}'" in fn, f"_pmaFailReasonText 缺 {reason} 分支"
    keys = set(re.findall(r"window\.Tf?\('(pma_(?:fr|up)_[a-z_]+)'", fn + s[s.index("function _pmaRenderUploadResult("):s.index("async function pmaDeleteItem(")]))
    assert keys, "没有 pma_fr_* / pma_up_* 词条引用？"
    missing_zh = sorted(k for k in keys if k not in zh)
    missing_en = sorted(k for k in keys if k not in en)
    assert not missing_zh and not missing_en, f"词条缺失 zh={missing_zh} en={missing_en}"
    # 人话不能是裸机器码（占位符齐平）
    for k in keys:
        assert set(re.findall(r"\{(\w+)\}", zh[k])) == set(re.findall(r"\{(\w+)\}", en[k])), k
