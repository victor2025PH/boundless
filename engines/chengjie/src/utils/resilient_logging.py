"""轮转容忍句柄占用的 RotatingFileHandler（2026-08-27 06:11 宕机事故沉淀）。

事故机制（boot_20260827_052822.err.log 取证）：外部工具（tmp_tail_log.py，给
173 座位做 ssh 日志监听）用普通 ``io.open`` 长持 ``app.log`` 读句柄——Windows 下
该句柄不带 FILE_SHARE_DELETE → ``RotatingFileHandler.doRollover`` 的 ``os.rename``
永久 WinError 32 → **每条日志** 都经 ``handleError`` 把整段 traceback 倒进 stderr
→ 与 okline SSE 零退避重试循环共振，45 分钟写满 1.5 GB boot err 日志，最终拖死
web 监听（坐席全断）。杀掉占用进程只是清了这一次的因；本模块让引擎对「谁又拿
只读句柄挂住了日志」这一整类环境事故免疫。

三层容忍（依次降级，**绝不向 emit 调用方抛异常**）：

1. 正常路径＝原生 ``doRollover``（rename 链，语义与行为零变化）；
2. rename 失败（句柄占用）→ **copytruncate**：先复制 base→``.1`` 再就地截断——
   不换 inode，任何持有句柄的读者都不受影响（logrotate 的 copytruncate 语义；
   截断瞬间的极少量并发写会丢，属日志可接受损耗。msvcrt 层 Python ``open`` 默认
   SH_DENYNO：占用者在场时「rename 必败、覆写截断可行」正是本降级成立的根据）；
3. copytruncate 也失败 → 进入冷却窗（默认 60s）：窗内 ``shouldRollover`` 直接
   返 0——把「每条日志重试一次轮转+报一次错」压成「每分钟至多一次」，
   stderr 风暴在结构上不再可能。

刻意不做：不改 ``handleError``（全局静音会把别的 handler 故障也藏掉）；
不引第三方 concurrent-log-handler（多一个依赖修一个已被上面三层覆盖的问题）。
"""

from __future__ import annotations

import os
import shutil
import time
from logging.handlers import RotatingFileHandler

__all__ = ["ResilientRotatingFileHandler"]


class ResilientRotatingFileHandler(RotatingFileHandler):
    """rename 被句柄占用挡住时降级 copytruncate、再失败进冷却的轮转处理器。"""

    #: copytruncate 也失败后，多久内不再尝试轮转（期间日志照常追加写入 base）
    cooldown_sec: float = 60.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._retry_after = 0.0

    # 冷却窗内直接跳过轮转判定：日志继续写 base（文件会暂时超过 maxBytes，
    # 属刻意选择——「日志略胖」远好于「每条日志向 stderr 倒一次堆栈」。
    def shouldRollover(self, record) -> int:  # noqa: ANN001 - 基类签名
        if time.time() < self._retry_after:
            return 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
            return
        except OSError:
            # rename 链被占用（含备份位移 .1→.2 任一环节）→ 降级 copytruncate
            pass
        try:
            dfn = self.rotation_filename(self.baseFilename + ".1")
            try:
                if os.path.exists(dfn):
                    os.remove(dfn)
            except OSError:
                pass  # .1 也被占用：copyfile 直接覆写内容，同效
            try:
                shutil.copyfile(self.baseFilename, dfn)
            except OSError:
                pass  # 快照失败不阻塞截断——保住主日志可写比保住备份重要
            # 就地截断（不换 inode）：占用者的句柄照旧有效
            with open(self.baseFilename, "w",
                      encoding=getattr(self, "encoding", None) or "utf-8"):
                pass
        except OSError:
            self._retry_after = time.time() + float(self.cooldown_sec)
        finally:
            # super().doRollover 在 rename 前已关流；无论走到哪个分支，
            # 都要把流恢复起来，否则下一条 emit 由 FileHandler 兜底重开。
            if not self.delay and self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    self._retry_after = time.time() + float(self.cooldown_sec)
