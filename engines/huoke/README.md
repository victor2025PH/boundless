# 智拓 ReachX（huoke / OpenClaw）已迁出本仓 — 2026-08-17

**代码不在这里了。别在这个目录下干活。**

| 你要找的 | 去哪里 |
|---|---|
| 智拓代码唯一真身 | `D:\projects\huoke`（工作区入口 junction：`C:\智拓`） |
| 远程仓库 | `github.com/victor2025PH/huoke`（私有；本机走 `git@github-push:victor2025PH/huoke.git`） |
| 迁出前的完整历史 | 本仓 git 历史（`git log b366901 -- engines/huoke`），最后状态=合流提交 `b366901` |
| 更早的深历史（boundless 收编以前） | 老仓 `victor2025PH/mobile-auto0423` |

迁出原因：本机曾有两个 boundless 克隆（`D:\boundless` 与已退役的 `D:\boundless_latest`），
智拓的开发在两边各做了一半形成分裂脑。2026-08-17 已做双侧工作树合流（W1+W2+messenger 66011cb）、
subtree split 保历史独立建仓、服务切至新路径（:18080 验收通过）。

服务自启：启动文件夹 `ReachX-Worker.lnk` → `D:\projects\huoke\.venv\Scripts\python.exe`
（**必须 python.exe 不能 pythonw.exe**——pythonw 无控制台句柄会让服务里几十处 adb 子进程调用
挂死超时，设备指纹采集 0.05s 变 67s，2026-08-17 迁移当天实锤）。
