# 开场页动效量化验收（Intro QA）

对象：首页全屏开场页（`components/IntroCover.tsx`）的两套动效——「产品 LOGO 从星门喷涌飞出」的粒子动效，与「进入 AI 世界」Siri 风格流光按钮。两者都不靠肉眼盯，用 Playwright 无头浏览器做量化验收，输出 PASS/FAIL 判定与 JSON 报告。

工具：`scripts/qa-intro-motion.mjs`（LOGO 粒子）、`scripts/qa-intro-button.mjs`（按钮）。粒子引擎在 `window.__blIntroStats` 上暴露实时统计对象（契约见 §4），motion 脚本轮询消费它；button 脚本直接对按钮 DOM 与交互行为断言。

## 1. 用途与范围

- **LOGO 粒子（qa-intro-motion.mjs）**：验收同屏去重、数量上限、左右交替与均衡、速度分布、飞行时长、出屏回收、喷涌吞吐等 10 项判定（§3.1），保证动效"快而不乱、涌而不断"。
- **Siri 按钮（qa-intro-button.mjs）**：验收 `.bl-enter-btn` 的 DOM 结构、指针跟随 CSS 变量、按压充能、点击冲越退场，并顺带回归粒子引擎存活与控制台零错误，共 6 项检查（§3.2）。
- 两者互相独立、各开各的浏览器页面，可单独跑也可用 `qa:intro` 串行跑。

## 2. 前置与用法

先启动站点服务（二选一），确保脚本 `--url` 指向的地址可访问：

```bash
npm run dev                       # 开发模式
npm run build && npm run start    # 生产构建 + 启动
```

再跑验收（package.json 已注册三条 scripts）：

```bash
npm run qa:intro           # 先粒子后按钮，串行两条
npm run qa:intro-motion    # 只跑 LOGO 粒子
npm run qa:intro-button    # 只跑 Siri 按钮
```

参数（两种写法 `--url x` 与 `--url=x` 均可，默认 URL 都是 `http://localhost:3470/`）：

| 参数 | 适用脚本 | 含义 | 默认值 |
| --- | --- | --- | --- |
| `--url` | 两者 | 被测页面地址 | `http://localhost:3470/` |
| `--ms` | qa-intro-motion | 观测时长（毫秒） | `12000` |
| `--headed` | qa-intro-motion | 有头模式（无 GPU 的 headless 会把 rAF 节流到 ~2fps，用它复核真实观感） | 关闭 |

单条脚本传参用 npm 的 `--` 透传：

```bash
npm run qa:intro-motion -- --url http://localhost:3000/ --ms 15000
npm run qa:intro-button -- --url http://localhost:3000/
```

注意：`qa:intro` 是 `a && b` 组合命令，npm 只会把 `--` 后的参数拼到**最后一条**命令上；要改 URL/时长时请分别跑两条，或直接让服务起在 3470（`npx next start -p 3470`）。

退出码（两脚本一致口径）：

- `0` = 全部 PASS；
- `1` = 存在 FAIL（或 button 脚本执行异常）；
- `2` = 环境不可用——motion 为页面打不开或 20s 内等不到 `window.__blIntroStats`；button 为 `.bl-enter-btn` 20s 内未可见。

输出：motion 最终打印格式化 JSON `{url, ms, metrics, checks, pass}`；button 每项检查实时打印一行 `{name, pass, detail}`，最终打印 `{url, checks, pass}`。

## 3. 检查项与阈值

### 3.1 qa-intro-motion.mjs（LOGO 粒子，10 项判定 + 1 项参考值）

脚本按固定间隔（120ms）轮询 `__blIntroStats`，观测 `--ms` 时长后结算：

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

| 名称 | 含义 | 判定 |
| --- | --- | --- |
| dom-structure | 流光按钮分层结构齐全 | 按钮内含 `.siri-halo`（呼吸光晕）/ `.siri-ring`（描边容器）/ `.siri-ring .flow`（旋转 conic 渐变流光）/ `.siri-glass`（玻璃高光层）/ `.label` / `.arrow` 六个子元素，缺一即 FAIL |
| hover-css-vars | 指针跟随 | 指针在按钮上移动时，内联样式更新 `--mx/--my`（百分比）与 `--rx/--ry`（deg，3D 倾斜）；左上/右下两处采样均非空且互不相同 |
| press-charging | 按压充能 | pointerdown 时按钮获得 `data-charging="1"`，pointerup 后清除 |
| intro-stats | 粒子引擎回归 | `window.__blIntroStats` 存在（放在点击检查之前，避免开场页退场后全局对象被清理） |
| click-warping | 点击冲越退场 | 点击按钮后 300ms 内 `#bl-intro` 的 class 包含 `warping`；点击会让开场页退场，故此项放在最后 |
| console-errors | 控制台干净 | console error 与 pageerror 共 0 条（忽略 Next dev 已知水合警告 "Extra attributes from the server"） |

## 4. `window.__blIntroStats` 契约

由粒子引擎在初始化时挂到 window、每 tick 原地更新，类型定义见 `components/IntroCover.tsx` 的 `BlIntroStats`。生命周期：仅在开场页展示且非 prefers-reduced-motion 时存在，开场页退场/组件卸载时 `delete`；开场页每会话只出现一次（sessionStorage `bl-intro-seen`），Playwright 每次全新上下文打开页面天然满足。

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
