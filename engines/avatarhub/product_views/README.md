# avatarhub 产品视图（四产品配置驱动裁剪）

> v1 目标：**同一引擎进程**按 `AVATARHUB_PRODUCT_ID` / overlay 裁剪**侧栏菜单与默认 Tab**。  
> **不是**多租户，**不是**数据隔离，**不**按产品拆进程内存储。

复用 chengjie 双实例经验：差异放在 **配置 overlay / env**，引擎热路径（口型/换脸/TTS）尽量不动。

## 四视图定位

| product_id | 品牌 | 主能力（capability_matrix） | 默认 Tab | 侧栏隐藏（hub.js id） |
|---|---|---|---|---|
| `huansheng` | 幻声 VoiceX | `voice_clone` | `voice` | `stream`, `interp` |
| `huanying` | 幻影 LiveX | `digital_human` / `live_faceswap` / `virtual_bg` … | `stream` | `sing`, `batch`, `interp` |
| `huanyan` | 幻颜 FaceX | `offline_faceswap` / `hair_preset` / `tryon` | `stream` | `clone`, `voice`, `sing`, `batch`, `interp` |
| `tongchuan` | 通传 VoxX | `interp` / `obs_subtitle`（+ 克隆音） | `interp` | `sing`, `batch`, `stream` |

真实侧栏键来自 `static/hub.js` 的 `tabs[].id`：

`profiles` · `clone` · `voice` · `sing` · `batch` · `dashboard` · `stream` · `interp` · `history` · `selfcheck` · `logs` · `settings`

深链：`/ui#<tab_id>`（`init()` 读 `location.hash`）。

## 如何启用

```powershell
$env:AVATARHUB_PRODUCT_ID = "huansheng"   # 或 huanying / huanyan / tongchuan
# 再按原方式启动 avatar_hub（默认 :9000）
```

- **未设置**或**未知值** → loader 返回 `mode=full`（不过滤，与今日行为一致）。
- 自检：`python engines/avatarhub/product_views/loader.py --selftest`

本地四视图试点步骤见仓库根 `deploy/instances/avatarhub_views.md`。

## 与 capability_matrix.json

- 每份 `*.yaml` 的 `capability_claims` 列出本视图对外主打的 claim id（与矩阵 `claims[].id` 对齐）。
- `feature_flags` 用布尔开关表达「本视图是否强调该能力」；**v1 不自动改矩阵、不挡 API**。
- 矩阵仍是营销宣称 ↔ 代码证据的门禁源；本目录只做 **UI 裁剪契约**。

## 与 products/*/product.yaml

| 视图 yaml | 产品薄封装 |
|---|---|
| `huansheng.yaml` | `products/huansheng/product.yaml`（engine: avatarhub） |
| `huanying.yaml` | `products/huanying/product.yaml` |
| `huanyan.yaml` | `products/huanyan/product.yaml` |
| `tongchuan.yaml` | `products/tongchuan/product.yaml` |

`product_yaml` 字段仅作映射注释；loader **不读取** `products/`（保持 products 只读、引擎侧自洽）。

## 逻辑能力组（幻颜）

侧栏没有独立的「离线换脸 / 发型 / 试衣」tab id。`huanyan.yaml` 因此在 `allowed_tabs` 里附带：

- `lab_offline_faceswap` / `lab_hair_preset` / `lab_tryon`

这些 **不是** hub.js 原生 id；接线时应用 `feature_flags` 控制面板显隐，不要把它们塞进侧栏 `tabs` 数组。详见 `APPLY.md`。

## 限制（v1）

1. **仅菜单 / 默认页 /（规划中的）功能旗标**；API、角色库、日志、授权数据仍共享同一进程视图。
2. **未接线前**：改 yaml / env **不会**改变 `/ui` 侧栏——必须按 `APPLY.md` 接到 hub。
3. 同机默认端口 **9000 共用**：同时只能跑一个 `PRODUCT_ID` 进程（除非改 `AVATARHUB_PORT`）。
4. 命令面板 / 首页 `FEATURE_REGISTRY`（`/api/features`）需另接同一 hide 列表，否则仍能从首页跳进已隐藏 Tab。

## 目录

```
product_views/
  README.md          ← 本文件
  APPLY.md           ← 接线点与补丁伪代码（v1 默认未改 avatar_hub.py）
  loader.py          ← env → yaml → 规范化 dict；--selftest
  huansheng.yaml
  huanying.yaml
  huanyan.yaml
  tongchuan.yaml
```
