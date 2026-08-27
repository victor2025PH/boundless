# 实施 42：speech_prints 实例 overlay（P1-1，2026-08-18）

> 三视角复盘（2026-08-18）定位的**用户侧最实堵点**：说话指纹唯一真相在
> `platform/spoken_style/data/speech_prints.json`——开发机是 git 跟踪件（运营改=
> 弄脏共享树），**打包态在只读安装目录**（付费客户给自建人设配指纹根本改不了）；
> WP-6 备货面板点名了缺口却只能给「复制模板自己贴」这个对打包态走不通的出口。

## 现状确认（按「先确认是否已开发」纪律）

- 包内已有：mtime 热加载（`colloquial_rewrite._prints()`）、`prompt_block`/
  `role_enabled` 消费、`data/conv_colloquial.flag` 灰度——**没有任何 overlay/
  数据区支持**，`_PRINTS_PATH` 是模块常量，无 env 覆写、无注入 API。
- 该文件是 avatarhub 线**字节拷贝件（勿本地改）**——注入必须在包外完成。

## 方案（深想后的取舍）

| 候选 | 判定 |
|---|---|
| 桥接层复刻指纹段模板 | ❌ 与 avatarhub 模板更新漂移，且 L4/role_enabled 吃不到 |
| 物化合并进出厂件路径 | ❌ 打包升级覆盖丢数据 / 开发机与 avatarhub git 流冲突 |
| **运行时重定向一个常量** | ✅ `colloquial_rewrite._PRINTS_PATH` → 数据区物化合并文件；包内 L1/L4/灰度/缓存失效全语义原样吃到合并视图，零复刻 |

落地物（门禁 `tests/test_speech_prints_overlay.py` 15 例）：

- `src/ai/speech_prints_overlay.py`：overlay=`config/speech_prints.local.json`
  （打包态可写、升级不丢、**WP-8 备份自动覆盖**）；物化=`speech_prints.runtime.json`
  （出厂 ∪ overlay，overlay 同键胜出；`_overlay_meta` 元键记双源指纹）；
  `ensure_runtime_file` 双 stat 保鲜（**出厂件手改照样即时生效**——avatarhub 线
  开发机直改出厂件的既有工作流不受影响）；`validate_entry` schema 对齐出厂件
  真实条目（print 必填/各字段上限/未知键拒收）。
- 桥接两处接线：`_load()` 包装载成功后 `install_redirect()`（幂等；包升级重命名
  `_PRINTS_PATH`=软降级回出厂行为 + 专项探测门禁点名）；`system_block` L1 热路
  `ensure_runtime_file()` 保鲜（新鲜时两次 stat + 一次小 JSON 读）。
- `POST /api/personas/{pid}/speech-print`：键=`resolve_spoken_name(persona)`
  逐字（与运行时同口径）；审计 `pmedia_speech_print_save`。
- 备货面板指纹行升级：模板 textarea **可编辑** + 「保存到本实例」按钮（兼容
  `{口称名: 条目}` 包装形解包）→ 保存即翻✓刷完成度；复制按钮保留。
- `persona_stock.load_speech_print_keys` 改读合并视图（出厂件缺席=包无消费者，
  指纹行维持不适用——不给「填了也不生效」的假入口）。

## 正金标（本批最重要的一条门禁）

`test_package_consumes_overlay_after_redirect`：save_entry 写 overlay →
`install_redirect()` → **包内 `prompt_block()` 真产出含 overlay 画像的指纹段**，
且出厂角色照常产出——证明的是「包吃到了」，不是「我写进文件了」。

## 收口现场（如实）

本批门禁 30+14 绿 + 前端门禁全绿（一轮瞬态红=撞上 sibling 半写保存，复跑即愈）；
路由清单红=sibling 在途 `my-notify-binding` ×3（`auth_user_routes.py` 2.9 分钟前
仍在编辑），非本批面。`.py` 随下窗装载；模板/i18n 已热更（面板保存按钮在端点
装载前会得到 404 提示文案，特性探测语义自洽）。

## 后续

- avatarhub 线同步一句：建议包侧正式支持 `SPOKEN_STYLE_PRINTS_PATH` env 覆写，
  重定向从 monkeypatch 升级为官方接口（现有探测门禁保软降级，不阻塞）。
- 实施39/规格 WP-6 的「复制模板」偏离记录已更新为本机制。
