# ui_regress — 坐席工作台视觉回归基线

UI 迭代的防回归工具：用**固定 mock 数据 + 冻结时钟 + 确定性渲染**对关键界面拍
逐位可复现的截图，与基线逐像素对比，超阈值即失败并产出红色高亮 diff 图。
同一 dev 实例连拍两次为 **0 像素差**（已实测），因此任何非零 diff 都值得看一眼。

## 依赖

- 本机跑着一个 chengjie dev 实例（默认 `http://127.0.0.1:18901`，POST /login 表单
  `auth_token=dev-ui-check`；可用 `--base-url` / `--token` 指到任意实例——页面
  HTML/CSS/JS 来自该实例，正是被回归的对象）
- `pip install playwright pillow`（playwright ≥1.45，需 `playwright install chromium`）

## 日常用法

```bash
cd tools/ui_regress
python capture.py          # 拍当前代码的截图 → shots/current/
python compare.py          # 与 baseline/ 对比；退出码 0=全过 1=有回归 2=环境错误
```

- `compare.py --threshold 0.5`：差异像素占比阈值（百分数，默认 0.5）。
- 失败时看 `shots/diff/<场景>.png`：差异像素纯红、其余压暗灰底，肉眼秒定位。
- `capture.py` 退出码：0=8 场景全成功；2=部分场景被跳过；3=全部失败（实例没起）。

## 更新基线（何时/怎么做）

**只在「有意的视觉改版」合入时**重建基线，让后续迭代以新视觉为准：

```bash
python make_baseline.py    # = capture 到 baseline/（覆盖同名图）
```

评审时把 baseline 图的变化一并过目（基线图纳入 git；`shots/` 被本目录
.gitignore 忽略）。若 make_baseline 输出里有 skip 场景，修复后必须重跑补齐，
否则 compare 会因 current 多图只给 warn、缺图给 FAIL 的口径不完整。

## 场景集（8 张）

dark / light × [收件箱空态 inbox_empty, 收件箱列表 inbox_list(mock),
聊天视图 chat_view(mock), 数据看板 dash]，viewport 1440×900、DSF=1。
文件名 `{theme}_{scene}.png`。

收件箱走 `/workspace?filter=all` 深链：mock 含 SLA 超时行时页面的“智能默认
筛选”会自动切到「超时」页签，深链把页签钉死在「全部」，6 条 mock 会话全量呈现。

## 确定性机制（改 capture.py 前必读）

连拍 0 差异靠以下五层叠加，破坏任何一层都会出现随机像素差
（表现为 compare 无改动也报红）：

1. **时钟冻结**：`pg.clock.set_fixed_time(BASE_TS=1700000000)`（timers 照常跑，
   页面加载不受影响），mock 的 last_ts / 消息 ts 均为 BASE_TS 的固定偏移 →
   「4分钟前 / 昨天 / 周一 / HH:MM」等相对时间文案恒定，看板右上角时钟也恒定。
2. **API 全拦截**：`/api/**` 全部由脚本 fulfill，页面呈现只由前端代码决定——
   - `chats` / `thread` / `dashboard` / `me` / `presence` / `my-perf` / `workload`
     / `escalations` / `checklist` / `risk-summary` → 固定 mock；
   - SSE（`/api/workspace/stream`、`/api/events`）→ 204，EventSource 关闭，
     不会有实时事件把列表/铃铛/toast 弄成随机态；
   - 头像代理（`/api/platforms/*/avatar*`）→ 404，`<img onerror>` 自移除，
     恒定回落「确定性渐变 + 首字母」头像（渐变色由名字哈希决定，天然稳定）；
   - 其余端点 → `{"ok":true}`，各面板恒定空态。
   （console 里的若干 404 即头像代理拦截所致，属预期。）
3. **禁动画/光标**：注入 `*{animation:none;transition:none;caret-color:transparent}`
   （含伪元素）；`#ws-toast-box` 整体隐藏——toast 是“加载后 N 秒出现、8 秒自灭”
   的赛跑元素，与截图时机存在竞态。
4. **一次性引导预置已读**：localStorage 预置 `ws_search_mode_hint_seen` /
   `ws_group_hint_seen` / `hl_onboard_dismiss`，否则“首次搜索模式提示” toast 与
   按钮脉冲只在第一次运行出现（首建基线时被抓过一次，之后 current 永远比基线少
   一块 → 2%+ 假差异）。
5. **渲染确定性 flags**：SwiftShader 软件光栅（`--disable-gpu
   --use-angle=swiftshader`）+ `--disable-partial-raster
   --disable-composited-antialiasing --disable-lcd-text
   --force-color-profile=srgb --hide-scrollbars` 等。GPU 光栅化下圆角/圆形
   （头像圈、徽章、状态点）的边缘 AA 存在帧间非确定性，同一 DOM 连拍两次也会
   漂移一两百像素；换软件光栅后实测归零。

## 已排除的动态元素一览（排查记录）

| 动态源 | 症状 | 冻结手段 |
|---|---|---|
| 相对时间「N分钟前」 | 每分钟全列表文字变化 | clock.set_fixed_time + 固定偏移 ts |
| 看板「团队在线状态」右上角时刻 | 每秒变化 | 同上（wsFmtTime(null) 吃冻结时钟） |
| SSE 实时推送 | 未读数/铃铛/toast 随机 | stream 拦截回 204 |
| 真实头像懒加载 | 随平台数据变化 | 头像代理 404 → 首字母头像 |
| 首次搜索模式提示 toast + 按钮脉冲 | 首跑与后续不同 | localStorage 预置已读 + toast 容器隐藏 |
| SLA 智能默认筛选 | 列表只剩超时会话 | `?filter=all` 深链钉死页签 |
| 输入框光标闪烁 | 半透明竖线时有时无 | caret-color: transparent |
| GPU 光栅 AA 抖动 | 圆形边缘 ±1px 噪点 | SwiftShader + 关部分光栅/合成 AA |

## 已知局限

- **依赖 dev 实例在线**：实例宕机/该页 500 时对应场景被跳过（capture 退出码
  2/3，输出点名哪张被跳过），工具本身不崩；实例恢复后重跑即可。18901 实例是
  开发用临时进程（`AITR_WEB_PORT=18901 AITR_WEB_TOKEN=dev-ui-check python
  main.py`），不在 watchdog 自愈范围内，死了要手动拉。
- **跨机器/跨环境基线不可迁移**：整图像素对比，字体库、浏览器（Chromium）
  版本、DPI 变化都会全局假阳——换环境后先 `make_baseline.py` 重建。
- **相对时间文案依赖冻结时钟**：若未来页面改用服务端渲染时间（Jinja 注入），
  clock API 冻不住，需在 mock/服务端侧另行固定。
- **mock 覆盖面**：仅列表/线程/看板主要端点给了有内容的固定数据，其余端点统一
  `{"ok":true}` → 面板呈现“恒定空态”。这保证确定性，但意味着这些面板的
  “有数据形态”不在回归保护内；新增场景时同步补 mock。
- **看板仅「今日」页签**：质量/经营/系统页签由前端 CSS 隐藏，未截图；需要时
  可加场景（点页签后再截）。
