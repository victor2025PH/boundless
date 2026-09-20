# 开场页动效量化验收（Intro QA）

对象：首页全屏开场页（`components/IntroCover.tsx`）——「产品 LOGO 从星门喷涌飞出」的粒子动效、「进入 AI 世界」Siri 风格流光按钮，以及 2026-07「片头化」改造后的展示/散场行为。都不靠肉眼盯，用 Playwright 无头浏览器做量化验收，输出 PASS/FAIL 判定与 JSON 报告。

**片头化行为（被测契约，2026-07 起）**：开场页从「阻断式闸门」改为「片头」——

- **移动端不出现**：`(pointer: coarse)` 或视口宽 `<768px` 时遮罩完全不展示（等同已看过：写 `sessionStorage bl-intro-seen` + 运行时标记），遮罩内 inline script 在 hydration 前按同一判定先行隐藏，防首帧闪现；
- **桌面自动散场**：展示 ~2.6s（入场 ~1.25s + 品牌停留 ~1.3s）后自动走与点击按钮相同的退场流程（prefers-reduced-motion 快速流程下 ~0.8s；自动路径不播 impact 撞击音——无用户手势会被自动播放策略拦截）；
- **任意点击/按键跳过**：遮罩整层可点（`role="button"`），Enter/空格/Escape 亦可；
- **QA 逃生舱**：URL 带 `?introhold=1` 时禁用自动散场（仅此一项，其余行为不变），供需要长时间观察遮罩的脚本使用。桌面两个脚本会对被测 URL **自动追加**该参数。

工具：`scripts/qa-intro-motion.mjs`（LOGO 粒子）、`scripts/qa-intro-button.mjs`（桌面按钮）、`scripts/qa-intro-button-ios.mjs`（移动端片头不出现）。粒子引擎在 `window.__blIntroStats` 上暴露实时统计对象（契约见 §4），motion 脚本轮询消费它；button 脚本直接对按钮 DOM 与交互行为断言；ios 脚本对「遮罩不出现且首屏直接可见」断言。

## 1. 用途与范围

- **LOGO 粒子（qa-intro-motion.mjs）**：验收同屏去重、数量上限、左右交替与均衡、速度分布、飞行时长、出屏回收、喷涌吞吐等 10 项判定（§3.1），保证动效"快而不乱、涌而不断"。
- **Siri 按钮（qa-intro-button.mjs）**：验收 `.bl-enter-btn` 的 DOM 结构、指针跟随 CSS 变量、按压充能、点击冲越退场，并顺带回归粒子引擎存活与控制台零错误，共 6 项检查（§3.2）。
- **移动端片头（qa-intro-button-ios.mjs）**：WebKit + iPhone 14 Pro 仿真，验收「遮罩不出现、首屏直接可见」共 6 项检查（§3.3）。旧版「波浪按钮」断言已随波浪形状 A/B 下线且移动端不再渲染按钮，一并移除。
- 三者互相独立、各开各的浏览器页面，可单独跑；`qa:intro` 串行跑桌面两条。

## 2. 前置与用法

先启动站点服务（二选一），确保脚本 `--url` 指向的地址可访问：

```bash
npm run dev                       # 开发模式
npm run build && npm run start    # 生产构建 + 启动
```

再跑验收（package.json 已注册四条 scripts）：

```bash
npm run qa:intro             # 先粒子后按钮，串行桌面两条
npm run qa:intro-motion      # 只跑 LOGO 粒子
npm run qa:intro-button      # 只跑 Siri 按钮
npm run qa:intro-button-ios  # 移动端片头不出现（默认打生产 https://bd2026.cc/）
```

参数（两种写法 `--url x` 与 `--url=x` 均可；桌面两脚本默认 URL 是 `http://localhost:3470/`，ios 脚本默认 `https://bd2026.cc/`）：

| 参数 | 适用脚本 | 含义 | 默认值 |
| --- | --- | --- | --- |
| `--url` | 三者 | 被测页面地址 | 桌面 `http://localhost:3470/`；ios `https://bd2026.cc/` |
| `--ms` | qa-intro-motion | 观测时长（毫秒） | `12000` |
| `--headed` | qa-intro-motion | 有头模式（无 GPU 的 headless 会把 rAF 节流到 ~2fps，用它复核真实观感） | 关闭 |

**`?introhold=1` 自动追加**：片头默认 ~2.6s 自动散场，桌面两脚本的断言都需要遮罩常驻，因此它们会对 `--url` 自动追加 `?introhold=1`（URL 已带该参数则不重复；最终生效 URL 会回显在报告 JSON 的 `url` 字段）。手动复核开场页时同理：浏览器直接开 `http://localhost:3470/?introhold=1` 即可让遮罩停住不自动散场。ios 脚本不需要——移动端遮罩本就不该出现。

单条脚本传参用 npm 的 `--` 透传：

```bash
npm run qa:intro-motion -- --url http://localhost:3000/ --ms 15000
npm run qa:intro-button -- --url http://localhost:3000/
```

注意：`qa:intro` 是 `a && b` 组合命令，npm 只会把 `--` 后的参数拼到**最后一条**命令上；要改 URL/时长时请分别跑两条，或直接让服务起在 3470（`npx next start -p 3470`）。

退出码：

- `0` = 全部 PASS；
- `1` = 存在 FAIL（或 button/ios 脚本执行异常）；
- `2` = 环境不可用（仅桌面两脚本）——motion 为页面打不开或 20s 内等不到 `window.__blIntroStats`；button 为 `.bl-enter-btn` 20s 内未可见。

输出：motion 最终打印格式化 JSON `{url, ms, metrics, checks, pass}`；button 与 ios 每项检查实时打印一行 `{name, pass, detail}`，最终打印 `{url, checks, pass}`（`url` 为实际生效地址，桌面两脚本含自动追加的 `introhold=1`）。ios 另存首屏截图到 `scripts/_ios_wave/01-first-screen.png`（目录名沿用旧版，复用 .gitignore 既有条目）。

## 3. 检查项与阈值

### 3.1 qa-intro-motion.mjs（LOGO 粒子，10 项判定 + 1 项参考值）

脚本按固定间隔（120ms）轮询 `__blIntroStats`，观测 `--ms` 时长后结算（被测 URL 自动带 `?introhold=1`，遮罩不会在观测中途自动散场；脚本全程无交互，不会触发整层点击跳过）：

| 名称 | 含义 | 阈值 |
| --- | --- | --- |
| dup | 同一 LOGO 不得同屏出现两份 | 每次轮询 live 内 key 互不重复，违规轮询数 = 0 |
| countCap | 同屏数量不超会话上限 | 每次轮询 `live.length ≤ cap` 且 `cap ≤ 7` |
| sideAlternate | 左右轮流出生 | spawns 按 t 排序后 side 严格 +1/-1 交替，违规数 = 0 |
| sideBalance | 左右均衡 | 可见数均值差 mean\|L-R\| ≤ 1.0，且 \|L-R\|>2 的尖峰轮询占比 ≤ 5%（均值/尖峰占比对慢跑者采样噪声稳健；交替性另由 sideAlternate 硬保证） |
| speedMedian | 整体速度感足够 | 速度中位数 ≥ 550 px/s |
| speedP25 | 偏慢的粒子也不拖沓 | 速度 p25 ≥ 320 px/s |
| spawnNoHover | 出生即在动，无原地悬停 | 速度样本最小值 ≥ 160 px/s |
| flightP90 | 单程飞行时间不冗长 | 飞行时长 p90 ≤ 1700 ms |
| exitOffscreen | 回收瞬间中心已越过屏幕边缘，无屏外空放 | `exits.offEdgePx` 的 p90 ≥ 0 |
| streamAlive | 喷涌流持续不断、有吞吐 | 观测期内 exits ≥ 8 条 |
| fpsInfo | (ticks 增量)/(观测秒数) | 不参与判定，仅打印参考（headless 下 rAF 被节流，数值失真） |

速度/时长四项（speedMedian / speedP25 / spawnNoHover / flightP90）在实现中为**解析式判定**：直接由引擎上报的闭式运动学参数计算——时间加权中位速度 `(v0+ve)/2`、时间轴 1/4 处速度 `v0+(ve-v0)/4`、计划飞行时长 `planMs`——帧率无关（详见 §5）；采样实测值另行打印在 `metrics.sampledForReference` 里仅供参考。

### 3.2 qa-intro-button.mjs（Siri 按钮，6 项检查，按执行顺序）

被测 URL 自动带 `?introhold=1`（hover/press 检查链耗时超过 2.6s 的自动散场窗口）。遮罩整层任意点击即跳过，但按钮 click 冒泡到整层只是再调一次幂等的 dismiss（空转），press-charging / click-warping 断言语义不变：

| 名称 | 含义 | 判定 |
| --- | --- | --- |
| dom-structure | 流光按钮分层结构齐全 | 按钮内含 `.siri-halo`（呼吸光晕）/ `.siri-ring`（描边容器）/ `.siri-ring .flow`（旋转 conic 渐变流光）/ `.siri-glass`（玻璃高光层）/ `.label` / `.arrow` 六个子元素，缺一即 FAIL |
| hover-css-vars | 指针跟随 | 指针在按钮上移动时，内联样式更新 `--mx/--my`（百分比）与 `--rx/--ry`（deg，3D 倾斜）；左上/右下两处采样均非空且互不相同 |
| press-charging | 按压充能 | pointerdown 时按钮获得 `data-charging="1"`，pointerup 后清除 |
| intro-stats | 粒子引擎回归 | `window.__blIntroStats` 存在（放在点击检查之前，避免开场页退场后全局对象被清理） |
| click-warping | 点击冲越退场 | 点击按钮后 300ms 内 `#bl-intro` 的 class 包含 `warping`；点击会让开场页退场，故此项放在最后 |
| console-errors | 控制台干净 | console error 与 pageerror 共 0 条（忽略 Next dev 已知水合警告 "Extra attributes from the server"） |

### 3.3 qa-intro-button-ios.mjs（移动端片头不出现，6 项检查，按执行顺序）

WebKit + iPhone 14 Pro 仿真（393×852、触摸屏 → `pointer: coarse` 与 `<768px` 两个移动判定都命中）。不追加 `introhold`——移动端遮罩本就不该出现：

| 名称 | 含义 | 判定 |
| --- | --- | --- |
| intro-hidden | 首个可断言时刻遮罩即不可见（防首帧闪现的可观测代理） | `#bl-intro` 不存在，或存在但 `display:none`（hydration 前 inline script 的合法中间态）；可见即 FAIL |
| intro-detached | React 校正后遮罩移出 DOM | 15s 内 `#bl-intro` 从 DOM 消失 |
| seen-marker | 移动端等同已看过 | `sessionStorage['bl-intro-seen'] === '1'` |
| scroll-unlocked | 不上滚动锁 | `body` 的 computed `overflow !== 'hidden'` |
| first-screen | 首屏正文直接可见 | `<main>` 存在且高度 >0，页面 `scrollHeight > innerHeight`（正文成型可滚）；另存首屏截图 |
| console-errors | 控制台干净 | console error 与 pageerror 共 0 条 |

「hydration 前不闪现」由遮罩内 inline script 保证（与组件同一移动判定，先于首帧把 `#bl-intro` 置 `display:none`）；脚本在事后只能断言状态而非首帧时序，intro-hidden 是其可观测代理，inline script 判定逻辑本身靠 code review 守住。

## 4. `window.__blIntroStats` 契约

由粒子引擎在初始化时挂到 window、每 tick 原地更新，类型定义见 `components/IntroCover.tsx` 的 `BlIntroStats`。生命周期：仅在开场页展示且非 prefers-reduced-motion 时存在，开场页退场/组件卸载时 `delete`；**移动端（pointer coarse 或 <768px 视口）遮罩不渲染、引擎不启动，该对象自始不存在**。开场页每会话只出现一次（sessionStorage `bl-intro-seen`），Playwright 每次全新上下文打开页面天然满足；桌面端长时间观察需 URL 带 `?introhold=1`（否则 ~2.6s 自动散场后对象被清理）。

顶层字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `t0` | number | 引擎启动时刻（`performance.now()` 时间轴，ms） |
| `ticks` | number | rAF 帧计数，引擎每 tick 自增（fpsInfo 的数据源） |
| `cap` | number | 本会话同屏粒子数上限（桌面 5~7 / 移动 4~5 / 低端机 ≤4，恒 ≤7） |
| `mobile` | boolean | 是否移动端视口（`max-width: 767px` 命中） |
| `lite` | boolean | 是否低端机降级（deviceMemory ≤ 4GB 或 ≤ 4 核） |
| `spawns` | 对象数组 | 出生事件环形缓冲（最多 120 条，超出移除最旧），元素字段见下表 |
| `exits` | 对象数组 | 回收事件环形缓冲（最多 120 条，超出移除最旧），元素字段见下表 |
| `live` | 对象数组 | 当前同屏存活粒子快照（每 tick 整体重建），元素字段见下表 |

`spawns[]` 元素（其中 v0/ve/planMs 为闭式运动学参数——出生速度/出屏末速/计划飞行时长，QA 靠它们做帧率无关的解析验收，真实浏览器中轨迹与这组参数严格一致）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `t` | number | 出生时刻（performance.now 时间轴，ms） |
| `key` | string | 产品 LOGO 标识 |
| `side` | 1 \| -1 | 出生侧（两侧严格交替） |
| `layer` | string | 景深层级 class（`bl-gl-far` / `bl-gl-mid` / `bl-gl-near`） |
| `v0` | number | 出生速度（px/s） |
| `ve` | number | 出屏末速（px/s） |
| `planMs` | number | 计划飞行时长（ms） |

`exits[]` 元素：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `t` | number | 回收时刻（performance.now 时间轴，ms） |
| `key` | string | 产品 LOGO 标识 |
| `flightMs` | number | 实测飞行时长（ms；节流环境含"最后一帧迟到"的回收延迟，QA 时长判定用 planMs） |
| `exitV` | number | 回收瞬间速度（px/s） |
| `offEdgePx` | number | 回收时粒子中心越过屏幕边缘的距离（px，≥0 即已出屏） |

`live[]` 元素：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `key` | string | 产品 LOGO 标识 |
| `x` / `y` | number | 当前中心坐标（px，视口坐标系） |
| `v` | number | 当前瞬时速度（px/s） |
| `scale` | number | 当前缩放系数 |
| `side` | 1 \| -1 | 出生侧 |

## 5. 注意事项

- **片头自动散场与 `?introhold=1`**：生产行为是桌面展示 ~2.6s 后自动散场（reduced-motion 下 ~0.8s；自动路径跳过 impact 撞击音，避免无手势播放被自动播放策略拦截报错）。桌面两脚本自动追加 `introhold=1` 规避；若手写新断言脚本忘了加，症状是「按钮/遮罩断言在 2~3s 后开始超时、`__blIntroStats` 突然消失」。
- **移动端视口跑桌面脚本必挂**：移动判定命中时遮罩不渲染——motion 会因等不到 `__blIntroStats` 而 `exit 2`，button 会因 `.bl-enter-btn` 不可见而 `exit 2`。移动端验收请用 qa-intro-button-ios.mjs（断言口径就是「不出现」）。
- **旧 A/B 实验已下线**：`intro_auto_enter`（B 桶无操作 12s 自动进入）被片头无条件自动散场取代并已从组件移除；脚本里原先钉桶用的 `localStorage ab_intro_auto_enter / ab_intro_btn_shape` 写入随之删除，时序确定性改由 `introhold=1` 保证。
- **prefers-reduced-motion**：该偏好下粒子引擎不启动（页面只静态散布装饰图标），`window.__blIntroStats` 不存在，qa-intro-motion.mjs 等待 20s 后 `exit 2` ——属预期行为，不代表页面故障。
- **无 GPU 的 headless 环境**：rAF 会被节流（约 2fps），`fpsInfo` 与 `metrics.sampledForReference`（采样实测速度/时长）仅供参考；速度/时长判定为解析式（基于 spawns 上报的 v0/ve/planMs 闭式参数），不受节流影响。需复核真实观感时用 `--headed` 有头模式。
- **点击类检查会触发开场页退场**：脚本顺序已处理——button 脚本把 intro-stats 放在点击检查之前、click-warping 放在最后；`qa:intro` 串行的两个脚本各开各的浏览器页面，互不影响。

## 6. CI 接入建议（GitHub Actions 示例）

流程：构建 → 后台 `next start -p 3470`（与脚本默认 URL 对齐）→ 等待端口就绪 → `npm run qa:intro`。以下 steps 片段仅作示例，接入时按仓库实际 workflow 改造：

```yaml
- name: 安装依赖
  run: npm ci

- name: 安装 Playwright 浏览器
  run: npx playwright install --with-deps chromium

- name: 构建
  run: npm run build

- name: 后台启动服务（端口 3470）
  run: npx next start -p 3470 &

- name: 等待端口就绪
  run: |
    for i in $(seq 1 30); do
      curl -sf http://localhost:3470/ >/dev/null && exit 0
      sleep 2
    done
    echo "3470 端口 60s 内未就绪" && exit 1

- name: 开场页动效验收
  run: npm run qa:intro
```

退出码即门禁：任一判定 FAIL 整个 step 非零退出、workflow 标红。headless Runner 上 `fpsInfo` 偏低属正常（见 §5），不影响判定结论。
