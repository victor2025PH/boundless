# 生产独立 checkout 切换手册(prod_checkout_runbook)

> 目标:把智聊/通译双实例的**代码加载源**从开发工作树 `D:\workspace\boundless`
> 切到生产专用 checkout `D:\boundless-prod`(只随 main 前进、只经 `git pull` 更新),
> 让开发树上的任何编辑(模板 Jinja 热重载、i18n pack 热重载、并行工作流保存)不再直接触达生产。
>
> 背景事故面:实例运行期从工作树热加载模板与词条(`auto_reload` + `web_i18n._maybe_reload`);
> 2026-07-20 实测:开发树上未提交的 packs API 改动(collect_packs 二元组→三元组)被生产实例
> 热重载捕获,fail-safe 回落单体字典(功能无损但丢 pack 键);同日 `git merge` 又因运行实例
> 持锁 `web_i18n.py` 而 unlink 失败。**代码源与编辑区必须分离。**

## 0. 前置事实(切换前逐条核实,不符先停)

| 事实 | 当前值 | 核实命令 |
|---|---|---|
| 生产 checkout 就绪 | `D:\boundless-prod`(main,克隆于 2026-07-20) | `cd D:\boundless-prod; git log --oneline -1` |
| 实例数据根(外置,不受影响) | `D:\chengjie-instances\{zhiliao,tongyi}\data` | `status_instances.ps1` |
| domains junction 指向**旧树** | `...\data\domains → D:\workspace\boundless\engines\chengjie\domains` | `Get-Item D:\chengjie-instances\<i>\data\domains \| Select Target` |
| watchdog 计划任务指向**旧树**脚本 | `D:\workspace\boundless\deploy\instances\watchdog_instances.ps1` | `Get-ScheduledTask Boundless-chengjie-watchdog \| % Actions` |
| 实例 overlay 无指向旧树的绝对路径 | 已核实(仅注释里出现历史路径) | `Select-String config.local.yaml -Pattern "D:\\\\workspace"` |
| 新 checkout i18n 合并视图健康 | zh/en 各 8190 键 | 见 §4 验证脚本 |

## 0.5 切换前置(2026-07-20 补充,实测发现)

1. **Baileys 依赖**:`D:\boundless-prod\engines\chengjie\services\whatsapp-baileys` 需先
   `npm install --no-audit --no-fund`(node_modules 被 gitignore,克隆不带;已于 2026-07-20 预装)。
2. **并行分支收敛**:切换会把生产代码源固定到 main——若 `feat/*` 分支上有**已提交未合并**的
   生产相关修复(实测案例:e5e3336 修 /api/drafts* 422),必须先合并进 main 再切,否则切换
   即回退该修复。核对命令:`git log origin/main..feat/<分支> --oneline`(应为空或全部非生产项)。
3. **独立进程一并迁移**:`tg_scan_portal.py`(扫码门户)与 Baileys sidecar(若在跑)也从旧树
   加载代码,切换窗口内一并从新树重启;计划任务清单逐个 `Get-ScheduledTask | % Actions` 核对。

## 1. 切换步骤(维护窗口 ~2 分钟/实例,金丝雀顺序:先 tongyi 后 zhiliao)

以 tongyi 为例(zhiliao 同理替换实例名):

```powershell
# ① 停(旧树脚本或新树脚本均可,防呆逻辑相同)
powershell -ExecutionPolicy Bypass -File D:\boundless-prod\deploy\instances\stop_instance.ps1 -Instance tongyi

# ② 重指 domains junction 到生产 checkout(先删旧 junction 再建,数据零拷贝)
$d = "D:\chengjie-instances\tongyi\data\domains"
(Get-Item $d).Delete()          # 只删 junction 本身,不动目标内容
New-Item -ItemType Junction -Path $d -Target "D:\boundless-prod\engines\chengjie\domains" | Out-Null

# ③ 从生产 checkout 启动(EngineDir 随脚本位置自动解析到 D:\boundless-prod)
powershell -ExecutionPolicy Bypass -File D:\boundless-prod\deploy\instances\start_tongyi.ps1 -DataDir D:\chengjie-instances\tongyi\data

# ④ 验收(11 项全 PASS 才继续下一实例)
powershell -ExecutionPolicy Bypass -File D:\boundless-prod\deploy\instances\verify_instance.ps1 -Instance tongyi -DataDir D:\chengjie-instances\tongyi\data
```

## 2. watchdog 计划任务改指新树(两实例都切完后)

```powershell
$a = (Get-ScheduledTask Boundless-chengjie-watchdog).Actions[0]
$new = $a.Arguments -replace [regex]::Escape('D:\workspace\boundless'), 'D:\boundless-prod'
Set-ScheduledTask -TaskName Boundless-chengjie-watchdog -Action (New-ScheduledTaskAction -Execute $a.Execute -Argument $new)
# 干跑核对(只探测不动手):
powershell -ExecutionPolicy Bypass -File D:\boundless-prod\deploy\instances\watchdog_instances.ps1 -NoSelfHeal
```

其余 `\Boundless\*` 任务(uploader/snapshot/purge 等)按同样 `-replace` 方式逐个检查改指;
`Boundless_*_DailyVerify` 若引用旧树脚本一并处理(`Get-ScheduledTask | % Actions` 逐个看)。

## 3. 今后发版流程(代码只经 git 进入生产)

```powershell
cd D:\boundless-prod
git pull --ff-only origin main       # 唯一更新入口;模板/词条改动热重载即生效
# 涉及 Python 代码的发版:按金丝雀顺序 stop/start 重启两实例(§1 ①③④,跳过 ②)
```

开发继续在 `D:\workspace\boundless`(或 worktree 短分支)进行,任何未提交编辑不再影响生产。

## 4. 验证与回滚

- 新 checkout i18n 快速体检:
  `cd D:\boundless-prod\engines\chengjie; python -c "import sys; sys.path.insert(0,'.'); from src.web.web_i18n import get_translations; print(len(get_translations('zh')))"`(应 ≥8000)
- **回滚** = 从旧树脚本重启 + junction 指回旧树(把 §1 中路径互换);旧树保持完整,随时可回。
- 切换判据:`status_instances.ps1` 双 GO + `verify_instance` 双 11 PASS + watchdog 干跑退出码 0。
