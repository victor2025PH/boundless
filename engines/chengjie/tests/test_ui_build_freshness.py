# -*- coding: utf-8 -*-
"""前端构建戳新鲜度门禁（2026-07-30，「忘 bump」探测器）。

背景
----
模板/静态资源热更新直达生产，但**开着的旧标签页仍跑旧 JS**——坐席要靠
`ui-build.txt` 首行变化触发「请刷新」横幅。这一步长期靠人记（协议写在
AGENTS/CLAUDE「前端批次双戳」条），忘了 = 「修好了坐席还在踩」（2026-07-29
账号 rail 事故根因；2026-07-30 品牌统一线一天内手工 bump 8 次，说明该步骤
高频且易漏）。本门禁把「人记得」变成「机器查」：

    坐席前端面（templates / static / shared/copilot）任何文件的 mtime
    比 ui-build.txt 新 ⇒ 有前端改动落地后没 bump ⇒ 红，
    并指路 `python scripts/bump_ui_build.py`。

口径与已知边界（docstring 即契约）：
  * 只看**坐席前端面**：src/web/templates/** + src/web/static/** +
    shared/copilot/**。后端 .py 不在内——刷新横幅解决的是「旧 JS/CSS」，
    路由行为变化由重启纪律管；
  * ui-build.txt 自身与 .gitignore 类噪音（__pycache__）排除；
  * **运行时数据目录排除**（2026-07-31）：static 下的 protocol_media/（协议
    媒体落盘）与 persona_avatars/（头像缓存）由**运行中的生产进程**持续写入，
    mtime 永远追着时钟跑，纳入观测=每来一条媒体消息就误报「忘 bump」（当日
    gate_sweep 实锤两次）。它们是数据不是前端代码，排除口径与打包一致
    （build_backend.py 的 static staging 同样剔除这两个目录）；
  * mtime 是启发式：git checkout/stash 会重写 mtime，可能造成一次性误报
    ——按报错指路 bump 一次即恢复，不影响不变量的方向正确性；
  * **中批红是诚实的**：另一条线改了模板还没收口时跑本门禁会红——那正是
    「生产已变、坐席标签页已陈旧」的真实状态；gate_sweep 属收口动作，
    收口时本就该 bump。
"""
from __future__ import annotations

import pathlib
import time

_REPO = pathlib.Path(__file__).resolve().parents[1]
STAMP = _REPO / "src" / "web" / "static" / "workspace" / "ui-build.txt"

WATCHED_ROOTS = (
    _REPO / "src" / "web" / "templates",
    _REPO / "src" / "web" / "static",
    _REPO / "shared" / "copilot",
    # 2026-08-27 补缺口：shared/assistant（小智球/教学/替我做三模块）与
    # shared/copilot 同样经 HTTP 直发坐席、同样热更即上生产，却一直不在观测面
    # ——改了小智前端忘 bump，坐席标签页照样跑旧 JS 且无人提醒。
    _REPO / "shared" / "assistant",
)

_EXCLUDE_PARTS = {"__pycache__", "protocol_media", "persona_avatars"}


def newest_frontend_file(roots=WATCHED_ROOTS, stamp: pathlib.Path = STAMP):
    """返回 (mtime, path)：受观测面里最新的文件（排除戳自身）。"""
    newest = (0.0, None)
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p == stamp:
                continue
            if _EXCLUDE_PARTS & set(p.parts):
                continue
            mt = p.stat().st_mtime
            if mt > newest[0]:
                newest = (mt, p)
    return newest


def test_ui_build_stamp_not_stale():
    """前端面最新文件不得比 ui-build.txt 新（新 ⇒ 忘 bump / 批次未收口）。"""
    assert STAMP.is_file(), f"缺 {STAMP}"
    stamp_mt = STAMP.stat().st_mtime
    newest_mt, newest_p = newest_frontend_file()
    assert newest_mt <= stamp_mt, (
        f"前端文件比构建戳新（最新：{newest_p} @ "
        f"{time.strftime('%m-%d %H:%M', time.localtime(newest_mt))}，"
        f"戳 @ {time.strftime('%m-%d %H:%M', time.localtime(stamp_mt))}）——"
        f"有前端批次落地后没 bump，开着的旧标签页不会收到刷新提醒。"
        f"修法：python scripts/bump_ui_build.py（若是另一条线的中批状态，"
        f"由收口方 bump；git checkout 造成的 mtime 重写按同法消警）"
    )


def test_detector_actually_detects(tmp_path):
    """探测器自证：新文件晚于戳必被抓；戳最新必放行（防「永远绿的摆设」）。"""
    root = tmp_path / "front"
    root.mkdir()
    stamp = tmp_path / "ui-build.txt"
    stamp.write_text("x\n", encoding="utf-8")

    f = root / "a.css"
    f.write_text("body{}", encoding="utf-8")
    late = time.time() + 60
    import os

    os.utime(f, (late, late))
    newest_mt, newest_p = newest_frontend_file(roots=(root,), stamp=stamp)
    assert newest_p == f and newest_mt > stamp.stat().st_mtime, "该抓未抓"

    os.utime(stamp, (late + 60, late + 60))
    assert newest_mt <= stamp.stat().st_mtime, "戳最新时应放行"

    # 戳自身在观测目录内也必须被排除（否则 bump 本身会自我触发）
    inner_stamp = root / "ui-build.txt"
    inner_stamp.write_text("y\n", encoding="utf-8")
    os.utime(inner_stamp, (late + 999, late + 999))
    mt2, p2 = newest_frontend_file(roots=(root,), stamp=inner_stamp)
    assert p2 == f, "戳自身应被排除在观测面外"

    # 运行时数据目录不算前端改动——生产进程持续往里写媒体，不该逼人 bump
    media = root / "protocol_media" / "whatsapp"
    media.mkdir(parents=True)
    mfile = media / "x.jpg"
    mfile.write_text("j", encoding="utf-8")
    os.utime(mfile, (late + 500, late + 500))
    _mt3, p3 = newest_frontend_file(roots=(root,), stamp=inner_stamp)
    assert p3 == f, "运行时媒体目录（protocol_media）应被排除在观测面外"
