# AvatarHub 四产品视图 — 本地试点

配置驱动裁剪脚手架：`engines/avatarhub/product_views/`（详见该目录 README）。  
**v2 已接线**（实施14）：`avatar_hub.py` 挂 `GET /api/product_view`、`hub.js` init 拉取过滤，
设 `AVATARHUB_PRODUCT_ID` 重启即生效，fail-open（异常回全菜单）；接线细节见 `product_views/APPLY.md`。

## 端口

| 项 | 说明 |
|---|---|
| 默认 | `AVATARHUB_PORT=9000`（与现网一致） |
| 同机多视图 | **同一端口同时只能一个进程**；换 `PRODUCT_ID` 须先停再建 |
| 真要并行 | 每进程不同 `AVATARHUB_PORT`（如 9000/9001/…），仍共享代码树；**数据目录默认不隔离**（非多租户） |

## 启用方式（同一脚本，不同 env）

在 `engines/avatarhub` 下（按你现网启动命令替换最后一行）：

```powershell
# 幻声
$env:AVATARHUB_PRODUCT_ID = "huansheng"
python avatar_hub.py

# 幻影
$env:AVATARHUB_PRODUCT_ID = "huanying"
python avatar_hub.py

# 幻颜
$env:AVATARHUB_PRODUCT_ID = "huanyan"
python avatar_hub.py

# 通传
$env:AVATARHUB_PRODUCT_ID = "tongchuan"
python avatar_hub.py
```

或四份薄封装（示例，可复制为 `start_huansheng.ps1` 等）：

```powershell
# deploy/instances/start_avatarhub_view.ps1 伪代码
param([ValidateSet('huansheng','huanying','huanyan','tongchuan')][string]$Product = 'huansheng')
$env:AVATARHUB_PRODUCT_ID = $Product
# 可选：$env:AVATARHUB_PORT = "9000"
Set-Location (Join-Path $PSScriptRoot "..\..\engines\avatarhub")
python avatar_hub.py
```

对照 chengjie：`CHENGJIE_PRODUCT_ID` 由 `start_zhiliao.ps1` / `start_tongyi.ps1` 注入；此处同理，**不要**把 `AVATARHUB_PRODUCT_ID` 写成机器级永久环境变量（换产品时易串味）。

## 自检（不启服务）

```powershell
cd D:\workspace\boundless\engines\avatarhub
python product_views\loader.py --selftest
python product_views\loader.py huansheng   # 打印规范化 JSON
```

## 裁剪摘要（接线生效后）

| PRODUCT_ID | 默认页 | 隐藏侧栏 |
|---|---|---|
| huansheng | 语音 | 开播、同传 |
| huanying | 开播 | 唱歌、批量、同传 |
| huanyan | 开播 | 克隆、语音、唱歌、批量、同传 |
| tongchuan | 同传 | 唱歌、批量、开播 |

## 与 chengjie 双实例差异

- chengjie：两进程 + 两数据根 + `config.local.yaml` overlay。  
- avatarhub 本试点：**一进程一视图**，只裁 UI；角色/日志仍同一默认路径，除非你另行设 `AVATARHUB_PROFILES_DB` 等（超出 v1 范围）。
