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

console.log("renderer-boot-invariants.test.js: " + passed + " passed");
