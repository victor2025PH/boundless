# 无界科技 BOUNDLESS · 品牌资产库

> 一个文件夹装下公司标识 + 三系七产品的全部头像、背景、组合标。
> 全部产物由 `build_brand_assets.py` 一键生成，改母版后重跑即可整库刷新。

## 品牌速览

| 层级 | 中文 | 英文 | 说明 |
|---|---|---|---|
| 母品牌 | 无界科技 | BOUNDLESS | ∞ 无限标，口号「让沟通，无界 / Communication, Boundless.」 |
| 智连系 Growth | 智拓 / 智聊 | ReachX / ChatX | 社交增长：获客 + AI 聊天成交（蓝色系光环） |
| 幻境系 Studio | 幻颜 / 幻声 / 幻影 | FaceX / VoiceX / LiveX | 数字分身：换脸 / 克隆声 / 直播分身（紫色系光环） |
| 通达系 Lingo | 通译 / 通传 | LingoX / VoxX | 跨语沟通：聊天翻译 / 同声传译（橙色系光环） |

品牌色（取自 ∞ 主标渐变）：`#00B0F0 → #1E6BF0 → #7A3BF5 → #D030F0 → #F0509A → #F07800 → #F0A010`；
深空底 `#1A1D3A → #05060F`；墨色文字 `#0B1020`。

字体：中文 Noto Sans CJK SC（Black/Bold/Medium，思源黑体同源，OFL 免费商用）；
英文 Montserrat（可变字重，OFL 免费商用）。字体文件在 `fonts/`，随库分发合法。

## 目录结构

```
brand-assets/
├─ build_brand_assets.py     一键重建脚本（python build_brand_assets.py）
├─ sync_brand_targets.py     分发产物到消费方：两份 website + 坐席工作台 + 桌面端
├─ apply_telegram_branding.py  线上应用：频道/群头像+简介（幂等，--dry-run 预演）
├─ MANIFEST.md               全部 106 个产物的清单（自动生成）
├─ fonts/                    品牌字体（OFL 授权，可商用可分发）
├─ 00_master/
│   ├─ src/                  白底母版（唯一需要人工维护的源；新产品放这里）
│   └─ keyed/                透明底母版（脚本产物，勿手改）
├─ 01_logos/
│   ├─ mark/                 公司 ∞ 主标 1024/512/256/128/64/32 透明底
│   ├─ mono/                 单色剪影（白/墨）— 水印、单色印刷、遮罩
│   └─ favicon/              boundless.ico（16–64 多尺寸）
├─ 02_product-icons/<key>/   7 产品图标 512/256/128 **正方形**透明底（统一 pad 8%）
├─ 03_lockups/
│   ├─ company/              公司组合标：横排 / 竖排 / 竖排+口号 × 墨字/白字
│   └─ products/             7 产品组合标（图标+中文+英文）× 墨字/白字
├─ 04_avatars/
│   ├─ company/              品牌主头像(深/浅)、频道光环版、客服徽标版(中/英)
│   └─ products/             7 产品 × (普通版 + 产品系光环版)，各 512 + 128 预览
└─ 05_backgrounds/           TG贴文 / 竖屏故事 / 桌面(深浅) / X / FB / 公众号(深浅)
                             / YouTube / 产品矩阵海报
```

## 账号怎么选头像（运营速查）

| 账号类型 | 用哪张 | 理由 |
|---|---|---|
| 公司官方号 / Bot | `company/avatar-brand-dark-512.png` | 纯主标，最大识别度 |
| 官方资源号 / 频道 | `company/avatar-channel-ring-512.png` | 渐变光环 = 官方资源号识别符 |
| 人工客服号（中文盘） | `company/avatar-support-zh-512.png` | 底部「客服」徽标，用户一眼认出真人客服入口 |
| 人工客服号（英文盘） | `company/avatar-support-en-512.png` | SUPPORT 徽标 |
| 产品专属号（如智聊 Bot） | `products/avatar-chatx-dark-512.png` | 产品图标当主体 |
| 产品资源号 / 素材频道 | `products/avatar-chatx-ring-512.png` 等 | 光环颜色 = 所属产品系（蓝智连/紫幻境/橙通达），矩阵感 |

背景速查：TG 频道贴图/置顶用 `tg-post-1280x720`；手机端引导页/朋友圈封面用
`story-1080x1920`；X 头图 `x-header-1500x500`；FB 封面 `facebook-cover-820x312`；
公众号次条封面 `wechat-banner-900x383-*`（900×383 ≈ 2.35:1）；YouTube 用
`youtube-banner-2560x1440`（文字已置于 1546×423 安全区）；给客户讲全线产品用
`matrix-poster-1920x1080` 一图讲清。

## 新增一个产品的 SOP

1. 生成同风格白底 3D 图标（参考现有 7 张的 prompt 风格：puffy 3D chrome、蓝紫橙渐变、白底），
   存为 `00_master/src/<key>-white.png`。
2. 在 `build_brand_assets.py` 顶部 `PRODUCTS` 里加一行（中文名/英文名/所属系/一句话描述）。
3. `python build_brand_assets.py` —— 图标多尺寸、组合标、两款头像、矩阵海报全部自动补齐。
4. 官网侧：把 `<key>-white.png` 拷到 `website/public/brand/products/` 并生成 256 透明版
   （或直接拷 `02_product-icons/<key>/<key>-256.png` 改名 `<key>.png`），
   在 `lib/brand.ts` + `components/productMeta.ts` 注册。

## 技术说明（为什么要有这个库）

- **抠图管线**：官网旧脚本 `build-boundless-marks.ps1` 按「像素接近白色→透明」抠图，
  会把图形内部的白色高光星芒抠穿成洞。本库用「洪水填充锁定外部背景 + 连通域面积区分
  结构镂空与高光」的算法，外部透明、结构镂空（∞ 内屏、面具眼口、靶环间隙）照旧，
  高光保留，边缘做了去白边处理（defringe），放在深色头像上无白色包边。
- **头像在小尺寸的可读性**：TG 会把头像裁圆并常显示为 40px 左右，所以头像版主体
  占比比官网图标版大（pad 16–26%），客服徽标做成大字胶囊而不是小字标签。
- **一致性**：所有中英文字排版（字号比例、字距、层级）都由脚本参数化，
  保证 106 个产物中英文排版规则完全一致，不靠人工对齐。
