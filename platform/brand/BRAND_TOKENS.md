# platform/brand · 设计令牌单一真相（Brand Tokens SSOT）

> **本目录是无界科技全线产品设计令牌的单一真相（Single Source of Truth）。**
> **本层零反向依赖：不 import 任何产品/引擎代码**——只被 `website / products / engines` 消费，绝不反向依赖它们（与 `platform/README.md` 依赖铁律一致）。
>
> 收敛来源：TG智控王 `tgkz2026/src/design-system/design-tokens.ts`（结构骨架）
> × 无界母品牌 `brand-assets/`（品牌色/字体）× `website/lib/brand.ts`（三系语义）。
> 产品命名/文案的单一真相仍在 `website/lib/brand.ts`，本目录只管**视觉令牌**，两者互补不重叠。

## 1. 文件清单

| 文件 | 角色 | 维护方式 |
|---|---|---|
| `tokens.json` | **唯一数据源**。品牌色（∞ 渐变 / 深空底 / 墨色）、三系光环色阶、语义色、灰阶、间距、圆角、阴影、排版、动效、层级、断点、明暗主题映射 | 手工维护（唯一允许改数值的文件） |
| `brand.css` | CSS 自定义属性导出：`:root { --bl-* }` + `[data-theme="dark"]` 翻转块 | 从 tokens.json 派生，改令牌后同步 |
| `tailwind-preset.cjs` | Tailwind preset（CommonJS），加载时 `require("./tokens.json")` 机械派生 | **零手写数值**，通常无需改动 |
| `BRAND_TOKENS.md` | 本契约文档 | 流程/口径变化时更新 |

约定：`tokens.json` 中以 `_` 开头的键（`_note` / `_sources` 等）是注释，消费方必须忽略（preset 的 `stripMeta` 已处理）。CSS 变量统一前缀 `--bl-`。

## 2. 三端如何消费

### Next.js 官网（Tailwind）

`website/tailwind.config.ts` 挂 preset，现有 `extend` 里与品牌重叠的 `colors.ink / colors.neon / fontFamily` 可逐步删除，改用 preset 提供的类：

```ts
import type { Config } from "tailwindcss";

const config: Config = {
  presets: [require("../platform/brand/tailwind-preset.cjs")],
  content: ["./app/**/*.{js,ts,jsx,tsx,mdx}", /* ... */],
  theme: { extend: { /* 仅放官网独有的装饰性扩展，如 grid-glow */ } },
};
export default config;
```

可用类示例：`bg-ink-950`、`text-growth` / `bg-studio-500` / `border-lingo-300`、`bg-brand-gradient`、`bg-ring-growth`、`shadow-primary`、`z-modal`、`ease-spring`。

### Angular / Electron（智控王等桌面端）

引入 `brand.css`，组件样式直接用变量：

```json
// angular.json → projects.<app>.architect.build.options.styles
["../platform/brand/brand.css", "src/styles.scss"]
```

```scss
.cta { background: var(--bl-growth-500); border-radius: var(--bl-radius-lg); }
body { background: var(--bl-bg); color: var(--bl-text); font-family: var(--bl-font-sans); }
```

深色模式：在 `<html>`（Electron 亦同）上切 `data-theme="dark"`，表层语义变量（`--bl-bg` / `--bl-text` / `--bl-border*`）自动翻转，无需在组件里写两套色值。

### Vue 后台

入口引入一次即可全局生效：

```ts
// main.ts
import "../platform/brand/brand.css";
```

若后台也用 Tailwind，则与官网相同：`presets: [require("../platform/brand/tailwind-preset.cjs")]`（两种方式可并存，数值同源）。

## 3. 冲突消解表（收敛时拍板的决定）

两套体系冲突处，一律**以无界母品牌为准**；结构性令牌以智控王为准（它最完整）：

| 冲突项 | 智控王原值 | 母品牌值 | 收敛结果 |
|---|---|---|---|
| 英文字体 | Inter | Montserrat（OFL） | **Montserrat**，字体文件随 `brand-assets/fonts/` 分发 |
| 中文字体 | Noto Sans TC（繁） | Noto Sans CJK SC | **Noto Sans CJK SC**（简繁全覆盖；web 回退 Noto Sans SC / PingFang SC / Microsoft YaHei） |
| 深色底 | slate-900/800/700（`#0f172a` 系） | 深空底 `#1a1d3a → #05060f` | **深空阶 `--bl-ink-950/900/800/700`**；slate 保留仅作冷灰备用，禁止再当暗底 |
| 主色 | 蓝 `#3b82f6` / 青 `#06b6d4` | 三系光环色 | 智控王属**智连系（growth）**，primary 对齐**智连蓝 `#1e8cf2`**（preset 已把 `primary` 别名指到 growth 色阶，`primary-*` 写法平滑迁移）；原辅色青并入智连光环渐变（`#0070f0→#00c2ff`） |
| 图标 | 智控王自有图标 | 三系产品图标（`brand-assets/02_product-icons/`） | **换用智连系图标**（智拓/智聊所在系），光环资源号头像用 `--bl-growth-ring` 同源渐变 |
| 浅底正文色 | gray-900 `#111827` | 墨色 `#0b1020` | **墨色 `--bl-ink-text`** |
| 语义色/间距/圆角/阴影/动效/层级/断点 | （完整） | （无此体系） | **沿用智控王原值**（阴影 `primary` 投影色随主色改为智连蓝） |

三系语义（与 `website/lib/brand.ts` 的 `accent` 字段同源）：

| 系 | 语义 | accent | 光环渐变 |
|---|---|---|---|
| growth 智连（智拓/智聊/智控王） | cyan 智连蓝 | `#1e8cf2` | `#0070f0 → #00c2ff` |
| studio 幻境（幻颜/幻声/幻影） | violet 幻境紫 | `#c43bf0` | `#b62bf5 → #f050c8` |
| lingo 通达（通译/通传） | amber 通达橙 | `#f07800` | `#f06a00 → #ffb020` |

未入令牌的智控王遗留：`componentVariants`（按钮/输入框尺寸表）与 `keyframes` 属组件库实现细节，留在各端组件层，不属于品牌令牌。

## 4. 新增 / 修改令牌的流程

1. **只改 `tokens.json`**（唯一数据源；新分组请带 `_note` 说明用途与来源）。
2. **同步 `brand.css`**：按命名规则补/改 `--bl-*` 变量（spacing 小数点写作 `-`，如 `0.5` → `--bl-space-0-5`；涉及明暗差异的，同时更新 `[data-theme="dark"]` 块）。
3. **检查 `tailwind-preset.cjs`**：它 require tokens.json 自动派生，通常不用动；只有新增**顶层分组**（比如加了 `opacity` 阶）才需要在 preset 里补一行映射。
4. **自检**：`node -e "console.log(require('./tailwind-preset.cjs').theme.extend)"` 能打印且无报错；抽查新值在 CSS 与 preset 中一致。
5. 三端各自升级引用即可，**禁止**在产品仓里复制数值或另开小灶色板；发现硬编码品牌色应回收进本目录。

## 5. 红线

- 本目录内**只放静态令牌**（JSON/CSS/preset/文档），不放运行时逻辑，不 import 任何产品/引擎代码。
- **不得**创建 `__init__.py`：顶层 `platform/` 与 Python 标准库 `platform` 同名，加包标记会遮蔽标准库，导致大量第三方库崩溃。
- 数值冲突时以 `tokens.json` 为准；`brand.css` / `tailwind-preset.cjs` 与其不一致视为 bug。
