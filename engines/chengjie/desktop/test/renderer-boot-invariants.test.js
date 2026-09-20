// renderer-boot-invariants.test.js — 「两个业务助手」防复发门禁（源码静态契约）
//
// 2026-08-03 安装版实锤事故：统一收件箱页自带业务助手右栏，而桌面壳 #copilot 的
// 隐藏逻辑只活在 activate() 里；启动路径 buildInboxTab 只置 active class、不经
// activate() → Option C 默认布局（rail 隐藏、无处可点）下壳侧栏开机常驻，与页内
// 面板并排成「两个业务助手」（壳侧空栏因无会话上下文永远是空壳）。
// 修复 = 启动时 inboxOn 就显式走一遍 activate(INBOX_ID)。
//
// renderer.js 是浏览器上下文大脚本（load 即触 document / window.shell），node 下
// 不可 require，故本门禁走源码静态契约（与主仓 python 侧「静态接线」门禁同哲学）。
// 若重构 activate()/启动路径，请让下列不变量继续成立，再同步这里的断言写法。
//
// 本文件同时挂在 `npm test` 与 predist / predist:win（构建安装包前置闸门）——
// 任何一半不变量丢失，安装包直接拒绝出生。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("renderer-boot-invariants FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const rendererDir = path.join(__dirname, "..", "renderer");
const rendererJs = fs.readFileSync(path.join(rendererDir, "renderer.js"), "utf8");
const styleCss = fs.readFileSync(path.join(rendererDir, "style.css"), "utf8");
const indexHtml = fs.readFileSync(path.join(rendererDir, "index.html"), "utf8");

// ① activate() 内「收件箱激活 → 隐藏桌面侧栏」双保险必须都在
ok(
  /classList\.toggle\(\s*["']cp-hidden["']\s*,\s*isInbox\s*\)/.test(rendererJs),
  'activate() 丢失 classList.toggle("cp-hidden", isInbox) —— 收件箱激活时桌面侧栏不再隐藏'
);
ok(
  /setProperty\(\s*["']display["']\s*,\s*["']none["']\s*,\s*["']important["']\s*\)/.test(rendererJs),
  "activate() 丢失内联 display:none !important 双保险（样式表规则可被覆盖，内联才兜底）"
);

// ② 启动路径必须显式走 activate：boot 不经点击，漏掉 = 开机即「两个业务助手」
ok(
  /if\s*\(\s*inboxOn\s*\)\s*activate\(\s*INBOX_ID\s*\)\s*;/.test(rendererJs),
  "启动路径丢失 `if (inboxOn) activate(INBOX_ID);` —— 安装版开机会重现「两个业务助手」"
);

// ③ CSS 兜底规则仍在（cp-hidden 类没有规则 = ① 的类切换白切）
ok(
  /#copilot\.cp-hidden\s*\{\s*display:\s*none\s*!important/.test(styleCss),
  "style.css 丢失 #copilot.cp-hidden { display:none !important } 规则"
);

// ④ 选择器契约：壳侧栏仍是 <aside id="copilot">（改 id 需同步 activate()/CSS/本测试）
ok(
  /<aside id="copilot">/.test(indexHtml),
  'index.html 丢失 <aside id="copilot">（activate()/CSS 按此 id 定位，漂移即全部失效）'
);

// ⑤ P2 账号栏健康三态接线（webmulti.railBadge → 每个 rail-item 一枚常驻角标点）：
//    模块写好没接上是本仓反复踩过的静默缺陷，四处缺一即断链。
ok(
  /<script src="webmulti\.js">/.test(indexHtml),
  "index.html 未加载 webmulti.js —— renderer.updateRailHealth 取不到 railBadge，栏点永远 idle"
);
ok(
  /function updateRailHealth\(/.test(rendererJs) && rendererJs.indexOf("railBadge(") > 0,
  "renderer.js 丢失 updateRailHealth/railBadge 接线（账号栏三态白接）"
);
ok(
  /InjectStatus\.byId\[wv\.dataset\.id\]\s*=\s*payload;[\s\S]{0,120}?updateRailHealth\(/.test(rendererJs),
  "onInjectStatus 收到上报后未刷 rail 健康点（每个号都该刷，不只当前聚焦）"
);
ok(
  rendererJs.indexOf('class="rail-dot') > 0,
  "addAccountTab 的 rail-item 未挂 .rail-dot 元素（无处可上色）"
);
ok(
  /\.rail-dot\.on\s*\{/.test(styleCss) && /\.rail-dot\.warn\s*\{/.test(styleCss)
    && /\.rail-dot\.off\s*\{/.test(styleCss) && /\.rail-dot\.idle\s*\{/.test(styleCss),
  "style.css 丢失 .rail-dot 三态色（on/warn/off/idle 任一缺失＝该态看不出区别）"
);

// ⑥ 2026-08-14 竖栏契约：#stage=两行两列网格（首行可选 #shell-notice 横跨两列，
//    第二行左=竖栏 #rail 右=#webviews）；#rail 顶部必须先有壳级品牌头 #rail-brand
//    （「菜单在 LOGO 下方」的落点）。其余任何元素插队仍算契约破裂。
const htmlNoComment = indexHtml.replace(/<!--[\s\S]*?-->/g, "");
ok(
  /<main id="stage">\s*(?:<div id="shell-notice"[\s\S]*?<\/div>\s*)?<nav id="rail"[^>]*>\s*<div id="rail-brand"[\s\S]*?<\/div>\s*<\/nav>\s*<div id="webviews">/.test(htmlNoComment),
  'index.html 的 #stage 结构漂移（应为 可选#shell-notice → <nav id="rail"> 内首元素 #rail-brand → #webviews）'
);
ok(
  /#stage\s*\{[^}]*display:\s*grid/.test(styleCss)
    && /#rail\s*\{[^}]*grid-column:\s*1/.test(styleCss)
    && /#webviews\s*\{[^}]*grid-column:\s*2/.test(styleCss),
  "style.css 丢失 #stage 网格分栏（rail 左列 / webviews 右列）——竖栏布局契约破裂"
);
ok(
  /#rail\s*\{[^}]*flex-direction:\s*column/.test(styleCss) && /#rail-brand\s*\{/.test(styleCss),
  "style.css 丢失 #rail 竖排方向或 #rail-brand 品牌头样式（LOGO 头下方竖栏语义破裂）"
);

// ⑦ 收件箱 webview 反向桥（页面「打开网页版」入口的全部依赖，缺一半就是死按钮）：
//    preload 接线 + chatx-bridge 监听/处理器 + preload 文件本体暴露 __chatxShell。
ok(
  /inbox-preload\.js/.test(rendererJs),
  "renderer.js 丢失收件箱 webview 的 inbox-preload.js 接线（页面拿不到 __chatxShell）"
);
ok(
  /chatx-bridge/.test(rendererJs) && /function onShellBridge\(/.test(rendererJs),
  "renderer.js 丢失 chatx-bridge ipc 监听/onShellBridge 处理器（页面『网页版』按钮点了无声无息）"
);
const preloadJs = fs.readFileSync(path.join(rendererDir, "inbox-preload.js"), "utf8");
ok(
  /__chatxShell/.test(preloadJs) && /sendToHost\(\s*["']chatx-bridge["']/.test(preloadJs),
  "inbox-preload.js 未暴露 __chatxShell 或未经 chatx-bridge sendToHost（桥断）"
);
ok(
  !/require\(\s*["']\.\.\//.test(preloadJs),
  "inbox-preload.js 出现 ../ 跨目录 require —— 装机版会 MODULE_NOT_FOUND（只许 require('electron')）"
);

// ⑧ 收件箱标签未读徽标链：renderer 徽标节点 + 桥 badge 分支 + CSS 规则，缺一即徽标永不显示。
ok(
  /rail-inbox-badge/.test(rendererJs) && /\.rail-badge\s*\{/.test(styleCss),
  "收件箱标签未读徽标断链（renderer 的 #rail-inbox-badge / style.css 的 .rail-badge 任一缺失）"
);

// ⑨ 标签条按需出现（rail-solo）：只剩收件箱时整条隐藏=与网页版零差异；
//    add/remove 路径必须回调 syncRailVisibility，否则「关掉最后一个标签后死条常驻」。
ok(
  /function syncRailVisibility\(/.test(rendererJs)
    && /#app\.rail-solo #rail\s*\{\s*display:\s*none/.test(styleCss),
  "rail-solo 按需隐藏断链（renderer.syncRailVisibility / style.css #app.rail-solo 任一缺失）"
);
ok(
  (rendererJs.match(/syncRailVisibility\(\)/g) || []).length >= 4,
  "syncRailVisibility 调用点不足（初始渲染/新增 Tab/via-inbox/移除 四处至少要各回调一次）"
);

// ⑩ 壳→页状态回推（P2）：pushShellState（标签增删经 syncRailVisibility / 健康变化经
//    updateRailHealth / 页面导航经 dom-ready 强推）→ preload ipc 中继 + getState 快照。
//    断链＝抽屉「已打开/健康」显示静默失效（回落到永远显示「打开」，不报错）。
ok(
  /function pushShellState\(/.test(rendererJs) && /function collectShellState\(/.test(rendererJs)
    && /chatx-bridge-state/.test(rendererJs),
  "renderer.js 丢失 pushShellState/collectShellState/chatx-bridge-state（状态回推断链）"
);
ok(
  /function updateRailHealth\([\s\S]{0,700}?pushShellState\(\)/.test(rendererJs)
    && /dom-ready[\s\S]{0,80}?pushShellState\(true\)/.test(rendererJs),
  "健康变化/页面导航未触发状态回推（updateRailHealth 内 pushShellState / dom-ready 强推 任一缺失）"
);
ok(
  /chatx-bridge-state/.test(preloadJs) && /getState/.test(preloadJs)
    && /chatx-shell-state/.test(preloadJs),
  "inbox-preload.js 状态中继断链（chatx-bridge-state 监听 / getState / chatx-shell-state postMessage 任一缺失）"
);
ok(
  /enabled:\s*EMBEDDED_ON/.test(rendererJs),
  "collectShellState 丢失 enabled 字段（Option C 关内嵌时页面无从隐藏入口 → 死入口回潮）"
);

// ⑪ Path2 assist-only 诚实契约（2026-08-13）：Messenger 等官方网页可聊但不接全自动。
//    断链＝再次「全自动运行中 + 今日 0」双谎 + 坐席以为网页登录=收件箱登录。
ok(
  /<script src="platform-caps\.js">/.test(indexHtml),
  "index.html 未加载 platform-caps.js —— assist-only 标签/诚实条/托管开关门控全部失效"
);
ok(
  /id="assist-only-banner"/.test(indexHtml) && /#assist-only-banner/.test(styleCss),
  "assist-only 诚实条 DOM/CSS 断链"
);
ok(
  /function syncAssistOnlyBanner\(/.test(rendererJs)
    && /syncAssistOnlyBanner\(/.test(rendererJs)
    && /currentIsAssistOnly\(/.test(rendererJs)
    && /assistOnly:\s*assist/.test(rendererJs),
  "renderer.js assist-only 接线缺失（banner / currentIsAssistOnly / feedActiveChat caps）"
);
ok(
  /via-tag assist/.test(rendererJs) || /via-tag assist/.test(styleCss),
  "人工副标 via-tag.assist 样式或挂载丢失"
);

// ⑧ 2026-08-13 管理台弹窗必须复用收件箱登录分区。webview/iframe 的 target=_blank
// 默认走壳 default session，没有 persist:backend-workspace 的 cookie → 管理台被
// _api_auth 打成 {"detail":"Unauthorized"}（Chromium JSON 预览）。
const mainJs = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
ok(
  /BACKEND_WORKSPACE_PARTITION\s*=\s*["']persist:backend-workspace["']/.test(mainJs),
  "main.js 丢失 BACKEND_WORKSPACE_PARTITION（管理台弹窗会丢登录 cookie）"
);
ok(
  /setWindowOpenHandler\(makeBackendPopupHandler\(\)\)/.test(mainJs),
  "main.js 未把 makeBackendPopupHandler 接到 webContents.setWindowOpenHandler"
);
ok(
  /did-attach-webview[\s\S]{0,400}?setWindowOpenHandler\(makeBackendPopupHandler\(\)\)/.test(mainJs),
  "main.js 未给内嵌 webview 接 setWindowOpenHandler（收件箱「打开管理台」仍会 401 JSON）"
);
ok(
  /partition:\s*BACKEND_WORKSPACE_PARTITION/.test(mainJs),
  "openBackendPopup 未指定 persist:backend-workspace 分区"
);

// ⑫ 2026-08-13 坐席机「两套工具箱」实锤：副驾 iframe 必须有后端就绪闸门 + 自愈重载。
//    坐席机后端由壳自拉起（冷启动几十秒），iframe 撞 ERR_CONNECTION_REFUSED 错误页后
//    「同值 src 不重载」＝永不自愈 → 看门狗回退原生 aside 后整个会话钉死旧面板，
//    与网页端/老板机长期呈现两套不同的工具箱。缺任一环，缺陷原样复发。
ok(
  /async function enableIframe\(\)[\s\S]{0,2600}?backendHealth\(\)/.test(rendererJs),
  "enableIframe 丢失 backendHealth 启动闸门（后端冷启动期 iframe 会加载失败且永不自愈）"
);
ok(
  /function navigateFrame\([\s\S]{0,400}?about:blank/.test(rendererJs),
  "navigateFrame 丢失 about:blank 归零两步导航（同值 src 不触发重载，失败错误页永久驻留）"
);
ok(
  /function scheduleFrameRetry\(/.test(rendererJs) && /function clearFrameRetry\(/.test(rendererJs),
  "scheduleFrameRetry/clearFrameRetry 缺失（后端就绪后无人重载 iframe，回退成永久态）"
);
ok(
  /disableIframe\(\);[\s\S]{0,600}?scheduleFrameRetry\(\)/.test(rendererJs),
  "看门狗回退分支未接就绪轮询（冷启动窗回退后再也回不到统一 App）"
);
ok(
  /else \{ clearFrameRetry\(\); disableIframe\(\); \}/.test(rendererJs),
  "手动切回原生未停就绪轮询（轮询会违背用户明示意愿强行切回 iframe）"
);

// ⑫b 冷启动占位叙事（P1 2026-08-13）：启动闸门期面板位必须有真实的启动叙事——
//    没有占位＝冷启动窗口坐席面对空白 iframe（旧原生面板此时同样取不到数据，
//    回退它不是答案）。三处接线缺一即哑：DOM / 冷启动分支展示 / cp-ready 收尾。
ok(
  /id="cp-boot-overlay"/.test(indexHtml) && /#cp-boot-overlay\s*\{/.test(styleCss),
  "index.html/style.css 丢失 cp-boot-overlay 占位层（冷启动窗回到空白面板）"
);
ok(
  /function setFrameBootOverlay\(/.test(rendererJs)
    && /setFrameBootOverlay\(true[\s\S]{0,120}?scheduleFrameRetry\(\)/.test(rendererJs),
  "enableIframe 冷启动分支未展示启动占位（setFrameBootOverlay(true…) + scheduleFrameRetry 接线缺失）"
);
ok(
  /if \(msg\.type === "cp-ready"\) \{[\s\S]{0,220}?setFrameBootOverlay\(false\)/.test(rendererJs),
  "cp-ready 未收尾启动占位（面板就绪后占位层会永久盖住统一 App）"
);

// ⑬ 执行类动作可见反馈（对齐网页宿主 P1-198）：cp-action-done 必须 flash 结果，
//    失败优先透传服务端原因——否则「创建任务/标记情绪」点了成功失败都无声（198 实录）。
ok(
  /cp-action-done[\s\S]{0,700}?flash\(d\.error \? String\(d\.error\)/.test(rendererJs),
  "renderer.js 的 cp-action-done 丢失结果 flash（执行失败被静默吞掉＝坐席眼里点了没反应）"
);
const appHtml = fs.readFileSync(
  path.join(rendererDir, "shared", "copilot", "app.html"), "utf8");
ok(
  /function cpToast\(/.test(appHtml) && /cp-action-done[\s\S]{0,500}?cpToast\(/.test(appHtml),
  "app.html 丢失 cpToast/cp-action-done 反馈接线（统一 App 宿主同样必须有结果出口）"
);

// ⑭ 冷启动埋点排队补发（P1 2026-08-13）：ui-event 纯 fire-and-forget 时，冷启动窗
//    的事件（启动闸门等待——恰是最需要观测的）发出时后端还没起来 → 永久丢失，
//    舰队遥测对坐席面板健康永远读到零（198 实测坐实）。缺队列＝盲区原样复发。
ok(
  /_uiEvtQueue/.test(mainJs) && /_scheduleUiEvtFlush/.test(mainJs)
    && /desktop:ui-event[\s\S]{0,900}?queued/.test(mainJs),
  "main.js ui-event 丢失冷启动排队补发（_uiEvtQueue/_scheduleUiEvtFlush/queued 任一缺失）"
);
ok(
  /cpshell_boot_gate_wait/.test(rendererJs) && /cpshell_boot_gate_load/.test(rendererJs),
  "renderer.js 丢失启动闸门配对埋点（boot_gate_wait/load——1.0.24 起舰队遥测的健康主信号）"
);

// ⑮ 壳级通知条（P0 2026-08-14）：更新就绪/官方公告的三点接线——HTML 元素、
//    preload 桥、renderer 消费。任一断＝「更新下完永远没人知道」原样复发
//    （1.0.22→1.0.26 实录：下载完成只写日志，用户端零提示，靠人肉 CloseMainWindow 才装上）。
ok(
  /id="shell-notice"/.test(indexHtml) && /id="sn-primary"/.test(indexHtml),
  "index.html 丢失 #shell-notice 通知条（更新就绪横幅无处渲染）"
);
const shellPreloadJs = fs.readFileSync(path.join(__dirname, "..", "shell-preload.js"), "utf8");
ok(
  /shellNotice:/.test(shellPreloadJs) && /updateRestart:/.test(shellPreloadJs)
    && /noticeAck:/.test(shellPreloadJs) && /onShellNotice:/.test(shellPreloadJs),
  "shell-preload.js 丢失通知桥（shellNotice/updateRestart/noticeAck/onShellNotice 任一缺失）"
);
ok(
  /initShellNotice/.test(rendererJs) && /updateRestart\(\)/.test(rendererJs),
  "renderer.js 丢失通知条消费（initShellNotice/一键重启接线）"
);
ok(
  /desktop:update-restart/.test(mainJs) && /update-downloaded/.test(mainJs)
    && /broadcastShellNotice/.test(mainJs),
  "main.js 丢失更新通知中心（update-restart IPC / update-downloaded → 广播链）"
);
ok(
  /#shell-notice\[hidden\]\s*\{\s*display:\s*none/.test(styleCss),
  "style.css 丢失 #shell-notice[hidden] 规则（display:flex 会压过 hidden 属性）"
);

// ⑯ 页内标签条接管（P2 2026-08-14，老板要求「状态栏移到工作台状态栏下面」）：
//    /workspace 页在自己头部下渲染 cx-shell-strip（数据=pushShellState），stripReady
//    握手后壳竖栏让位。四点接线缺一＝要么标签条不出现、要么「rail 藏了页里又没条」死路：
//    ① state 带 strip 能力位（旧壳不带 → 热更页面零变化）；② onShellBridge 处理 stripReady
//    且 dom-ready 归零（页面导航后必须重新握手）；③ CSS rail-page-strip 让位规则；
//    ④ preload 暴露 stripReady() + openEmbedded 认 shell_tab_id 精确点名。
ok(
  /strip:\s*true/.test(rendererJs),
  "collectShellState 丢失 strip 能力位（页面永远不渲染页内标签条）"
);
ok(
  /msg\.cmd === "stripReady"[\s\S]{0,200}?_pageStripLive = true/.test(rendererJs)
    && /dom-ready[\s\S]{0,200}?_pageStripLive = false/.test(rendererJs),
  "stripReady 握手断链（收到不置位 / 页面导航不归零——竖栏兜底语义破裂）"
);
ok(
  /function syncPageStripTakeover\(/.test(rendererJs)
    && /#app\.rail-page-strip #rail\s*\{\s*display:\s*none/.test(styleCss),
  "rail-page-strip 让位断链（syncPageStripTakeover / style.css 规则任一缺失）"
);
ok(
  /stripReady\(\)/.test(preloadJs) && /shell_tab_id/.test(rendererJs),
  "preload stripReady() 或 openEmbedded shell_tab_id 点名缺失（页内标签条点了切不动/握手无从发出）"
);

// ⑰ 开机动画（splash P0 2026-08-22）：赛博朋克首启序列的三线接线——
//    DOM/CSS/驱动三件套 + renderer 状态镜像事件桥 + 主进程防白闪。任一断＝
//    要么开机黑屏/白闪回归、要么 splash 等不到收幕事件把人永久困在动画后面。
const splashCss = fs.readFileSync(path.join(rendererDir, "splash.css"), "utf8");
const splashJs = fs.readFileSync(path.join(rendererDir, "splash.js"), "utf8");
const shellI18nSrcForSplash = fs.readFileSync(path.join(rendererDir, "shell-i18n.js"), "utf8");
ok(
  /<div id="bl-splash"/.test(indexHtml)
    && indexHtml.indexOf('id="bl-splash"') < indexHtml.indexOf('<div id="app">'),
  'index.html 丢失 #bl-splash 或未置于 #app 之前（首帧必须先画动画层）'
);
ok(
  /<link rel="stylesheet" href="splash\.css"/.test(indexHtml)
    && /<script src="splash-model\.js">/.test(indexHtml)
    && /<script src="splash\.js">/.test(indexHtml),
  "splash 三件套（splash.css / splash-model.js / splash.js）未全部接入 index.html"
);
ok(
  indexHtml.indexOf('<script src="splash-model.js">') < indexHtml.indexOf('<script src="splash.js">'),
  "splash-model.js 必须先于 splash.js 加载（驱动层依赖 window.splashModel）"
);
ok(
  /#bl-splash\s*\{[^}]*z-index:\s*99900/.test(splashCss),
  "splash z-index 漂移（须 99900：低于首启向导 99999/海报 99980——引导叠在动画上交互，高于其余一切）"
);
ok(
  /spAutoHide/.test(splashCss) && /prefers-reduced-motion/.test(splashCss),
  "splash.css 丢失 150s 自动隐藏保险（脚本没跑起来时的唯一逃生门）或 reduced-motion 降级"
);
ok(
  /aitr:inbox-phase/.test(rendererJs) && /aitr:splash-retry/.test(rendererJs),
  "renderer.js 丢失 splash 状态镜像事件桥（aitr:inbox-phase 派发 / aitr:splash-retry 重试回派）"
);
ok(
  /if \(!inboxOn\)[\s\S]{0,300}?aitr:inbox-phase/.test(rendererJs),
  "renderer.js 丢失「收件箱未启用 → 立即通知 splash 收幕」（缺了＝纯内嵌模式开机永久卡动画）"
);
ok(
  /aitr:inbox-phase/.test(splashJs) && /aitr:splash-done/.test(splashJs)
    && /aitr:splash-retry/.test(splashJs),
  "splash.js 事件桥断链（消费 aitr:inbox-phase / 派发 aitr:splash-done / 重试回派任一缺失）"
);
ok(
  /try \{ init\(\); \} catch/.test(splashJs) && /style\.animation = "none"/.test(splashJs),
  "splash.js 丢失异常拆层守卫（init try/catch）或未接管 CSS 自动隐藏保险（跑起来就该关掉它）"
);
ok(
  /backgroundColor:\s*"#05060f"/.test(mainJs) && /show:\s*false/.test(mainJs)
    && /ready-to-show/.test(mainJs),
  "main.js 防白闪三件套缺失（backgroundColor 深空底 / show:false / ready-to-show 再亮窗）"
);
ok(
  /second-instance/.test(mainJs)
    && /_focusExistingWindow/.test(mainJs)
    && /second-instance createWindow/.test(mainJs)
    && /_windowBootStarted/.test(mainJs)
    && /no window 12s after taking single-instance lock/.test(mainJs),
  "main.js 单实例无窗恢复缺失（二次启动须能补开窗；持锁 12s 无窗须退出放锁——2026-09-12 117 无窗僵尸）"
);
ok(
  (shellI18nSrcForSplash.match(/'splash\.amb\.engine\.0':/g) || []).length === 2,
  "shell-i18n.js 丢失「唤醒量子计算机群」氛围词条（zh/en 各一处）"
);

// ⑱ 融合标题栏（titlebar merge P2 2026-08-22）：win32 隐藏系统标题栏后，壳级 32px
//    细条承接「拖拽面 + 原生窗控让位 + ⋯应急菜单（白屏时的报障/更新保命通道）」。
//    七点接线缺一：要么窗口没处可拖、要么原生按钮悬空盖内容、要么白屏时报障无门。
ok(
  /<div id="cx-titlebar">/.test(indexHtml)
    && indexHtml.indexOf('id="cx-titlebar"') > indexHtml.indexOf('id="bl-splash"')
    && indexHtml.indexOf('id="cx-titlebar"') < indexHtml.indexOf('<div id="app">'),
  'index.html 丢失 #cx-titlebar 或位置漂移（须在 #bl-splash 之后、#app 之前）'
);
ok(
  /id="cx-tb-more"/.test(indexHtml) && /data-tbm="about"/.test(indexHtml)
    && /data-tbm="diag"/.test(indexHtml) && /data-tbm="update"/.test(indexHtml),
  "index.html 丢失细条⋯应急菜单三件（about/diag/update——webview 白屏时唯一报障入口）"
);
ok(
  /#cx-titlebar\s*\{\s*display:\s*none/.test(styleCss)
    && /body\.cx-tb-on #cx-titlebar\s*\{[^}]*-webkit-app-region:\s*drag/.test(styleCss)
    && /body\.cx-tb-on #app\s*\{\s*height:\s*calc\(100vh - 32px\)/.test(styleCss)
    && /body\.cx-tb-on #bl-splash\s*\{\s*top:\s*32px/.test(styleCss),
  "style.css 融合标题栏规则缺失（默认隐藏/拖拽面/#app 让高/splash 让位 任一丢＝布局或拖拽断）"
);
ok(
  /#cx-tb-menu\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(styleCss),
  "style.css 丢失 #cx-tb-menu[hidden] 兜底（display:flex 会压过 hidden 属性——⋯菜单关不上）"
);
ok(
  /function initTitlebar\(/.test(rendererJs)
    && /get\("tb"\)\s*===\s*"1"/.test(rendererJs)
    && /cx-tb-title/.test(rendererJs),
  "renderer.js 丢失 initTitlebar（?tb=1 点亮 / 细条标题镜像）——细条永远不显示或标题恒空"
);
ok(
  /titlebarMenu:/.test(shellPreloadJs) && /onTitlebarTheme:/.test(shellPreloadJs),
  "shell-preload.js 丢失细条桥（titlebarMenu/onTitlebarTheme 任一缺失＝⋯菜单/主题换肤断链）"
);
// ⑱a 全屏收起链（P2b）：F11 全屏后原生窗控消失，细条必须同步收起（否则 32px 死空间）。
ok(
  /onTitlebarFs:/.test(shellPreloadJs)
    && /cx-titlebar-fs/.test(mainJs)
    && /enter-full-screen/.test(mainJs)
    && /cx-tb-fs/.test(rendererJs)
    && /body\.cx-tb-on\.cx-tb-fs #cx-titlebar\s*\{\s*display:\s*none/.test(styleCss)
    && /body\.cx-tb-on\.cx-tb-fs #app\s*\{\s*height:\s*100vh/.test(styleCss),
  "全屏收起链断裂（main enter-full-screen 推送 / preload onTitlebarFs / renderer cx-tb-fs / CSS 收起+还原 任一缺失）"
);
ok(
  /titleBarStyle\s*=\s*"hidden"/.test(mainJs)
    && /titleBarOverlay\s*=\s*\{/.test(mainJs)
    && /desktop:titlebar-menu/.test(mainJs)
    && /cx-titlebar-theme/.test(mainJs)
    && /tb:\s*TB_MERGED\s*\?\s*"1"\s*:\s*"0"/.test(mainJs),
  "main.js 融合标题栏半件缺失（titleBarStyle/Overlay / titlebar-menu IPC / 主题回推 / ?tb 点亮参 任一丢）"
);
// ⑱b 开发者工具全员隐藏（老板令 2026-08-22）：原生菜单与页内 spec 默认清单都不得
//     再出现 toggleDevTools / devtools 项；唯一入口＝页面「版本号连点 12 次」解锁态
//     （workspace_base 注入，动作走 desktop:app-menu-action 的 devtools 分支——分支必须保留）。
ok(
  !/role:\s*"toggleDevTools"/.test(mainJs),
  'main.js 原生菜单出现 role:"toggleDevTools"（开发者工具须对所有人隐藏，含 Alt 后门菜单）'
);
ok(
  !/it\("devtools"/.test(mainJs),
  'main.js appMenuSpec 默认清单出现 devtools 项（只许页面解锁态注入，spec 里不发）'
);
ok(
  /case "devtools":/.test(mainJs),
  "main.js app-menu-action 丢失 devtools 分支（解锁态菜单项点了无响应）"
);
ok(
  /version:\s*displayVersion\(\)/.test(mainJs) && /devmode_hint/.test(mainJs),
  "main.js appMenuSpec 丢失 version/extras（帮助菜单版本行与解锁词条无源——彩蛋整链哑掉）"
);
// ⑱c 用户端 DevTools 封死（老板令 2026-09-19）：打包态一律不可开，源码态不随 --dev 自动弹。
//     ① 准入常量必须绑定 !app.isPackaged；② 每个 BrowserWindow webPreferences 与
//     will-attach-webview 都下 devTools 准入；③ 自动弹窗只认 --devtools；④ devtools 分支
//     在不准入时回执 devtools_disabled；⑤ spec 带 devtools_allowed 让页面不接线解锁。
ok(
  /const DEVTOOLS_ALLOWED\s*=\s*!app\.isPackaged;/.test(mainJs),
  "main.js 丢失 DEVTOOLS_ALLOWED = !app.isPackaged（用户端 DevTools 准入常量）"
);
ok(
  (mainJs.match(/devTools:\s*DEVTOOLS_ALLOWED/g) || []).length >= 3
    && (mainJs.match(/new BrowserWindow\(/g) || []).length === (mainJs.match(/devTools:\s*DEVTOOLS_ALLOWED/g) || []).length,
  "main.js 有 BrowserWindow 的 webPreferences 未下 devTools: DEVTOOLS_ALLOWED（用户端 DevTools 可被打开）"
);
ok(
  /will-attach-webview[\s\S]{0,400}webPreferences\.devTools\s*=\s*DEVTOOLS_ALLOWED/.test(mainJs),
  "main.js will-attach-webview 未给 webview guest 下 devTools 准入（工作台/官方页 webview 可开 DevTools）"
);
ok(
  !/argv\.includes\("--dev"\)/.test(mainJs)
    && /const DEVTOOLS_AUTO_OPEN\s*=\s*DEVTOOLS_ALLOWED\s*&&\s*process\.argv\.includes\("--devtools"\)/.test(mainJs)
    && /if \(DEVTOOLS_AUTO_OPEN\) win\.webContents\.openDevTools/.test(mainJs)
    && (mainJs.match(/openDevTools\(/g) || []).length === 1,
  "main.js DevTools 自动弹窗回归：只许 --devtools（且源码态）触发，唯一一处 openDevTools"
);
ok(
  /case "devtools":[\s\S]{0,300}if \(!DEVTOOLS_ALLOWED\) return \{ ok: false, error: "devtools_disabled" \}/.test(mainJs),
  "main.js app-menu-action devtools 分支丢失打包态拒绝回执（devtools_disabled）"
);
ok(
  /devtools_allowed:\s*DEVTOOLS_ALLOWED/.test(mainJs),
  "main.js appMenuSpec 丢失 devtools_allowed（页面无从得知用户端不许解锁，会注入点不开的 DevTools 项）"
);

console.log("renderer-boot-invariants.test.js: " + passed + " passed");
