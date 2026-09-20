// uninstall-nsh-invariants.test.js —— 卸载器「数据处置」contract 的静态看门人
//
// build/installer.nsh 给卸载器加了「保留数据（默认）/ 彻底删除」页面与真删逻辑。
// NSIS 没有单测框架、错误只能在打包甚至用户机上暴露，所以把五条契约（C1-C5，
// 见 nsh 头注释）降维成源码文本不变量在这里钉死——违反任何一条，安装包拒绝出生
// （挂 npm test 与 predist* 三链）。
//
// 血泪对应表：
//   C1 isUpdated 硬守卫在一切 RMDir 之前 —— 升级链误删用户数据=灾难级，双保险
//      的「第二根保险丝」就是这条顺序断言。
//   C2 --delete-app-data 显式参数在场 —— 静默默认保数据（uninstall_chatx_node.ps1
//      的 /S 语义不变），运维清数据必须显式说出来。
//   C3 删除目标只许宏路径 —— RMDir 行出现 CJK 字面量（如「智聊」）意味着有人绕开
//      ${APP_FILENAME}，PS5.1-GBK 家族教训在 NSIS 同样成立。
//   C4 进程收割必须排除 '*Uninstall*' —— `_?=` 原地运行时按路径杀进程会把卸载器
//      自己杀掉（wipe 进行到一半死在现场）。
//   C5 删后校验 + 如实报告 —— 「半套数据库」比全旧更糟，锁死残留必须点名。
//   编码：文件必须 UTF-8 with BOM —— makensis 对无 BOM 文件按系统 ACP(GBK) 解码，
//   中文 LangString 全部乱码（编辑器一次「优化保存」就能弄丢 BOM，所以钉住）。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("uninstall-nsh FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const appDir = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf8"));
const nshPath = path.join(appDir, "build", "installer.nsh");

// ---- 挂载点：include 已配置且文件存在 ---------------------------------------
const nsis = (pkg.build && pkg.build.nsis) || {};
ok(nsis.include === "build/installer.nsh", "package.json build.nsis.include 必须指向 build/installer.nsh（当前: " + nsis.include + "）");
ok(fs.existsSync(nshPath), "build/installer.nsh 不存在");

// ---- 语言集收敛：LangString 只写 zh/en，语言集不收敛会在 warningsAsErrors 下
//      产生几十条 "LangString not set" 直接打包炸掉 -------------------------------
const langs = nsis.installerLanguages;
ok(
  Array.isArray(langs) && langs.length === 2 && langs.includes("zh_CN") && langs.includes("en_US"),
  "build.nsis.installerLanguages 必须是 [en_US, zh_CN]（LangString 只有中英两套）"
);
// 顺序即回落语言：NSIS 按系统语言精确/主语言匹配，匹配不到就用第一个加载的语言文件。
// 中文系统仍精确命中 zh_CN（繁体按主语言命中简体），越南/泰/印尼等系统回落英文——
// 之前 zh_CN 在前，海外非英语系统拿到的是全中文向导且无处切换（2026-09-05 round 4）。
ok(langs[0] === "en_US", "installerLanguages 第一项必须是 en_US（不支持的系统语言回落英文而不是中文）");

// ---- 编码：UTF-8 BOM ---------------------------------------------------------
const raw = fs.readFileSync(nshPath);
ok(raw.length > 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf, "installer.nsh 丢失 UTF-8 BOM（makensis 将按 GBK 解码，中文文案会乱）");
const text = raw.toString("utf8").replace(/^\uFEFF/, "");

// ---- 三个宏齐备 --------------------------------------------------------------
// 进程家族谓词与 PowerShell 前缀是 !define 单一事实源（两条命令必须同口径——「杀什么」
// 与「验什么」一旦漂移，就是 2026-09-20 一轮那种「网早就空转了却没人发现」）。断言
// 要看展开后的实际命令，所以这里把 define 先内联回去。
function expandDefines(s) {
  for (const name of ["CX_FAM_FILTER", "CX_PS"]) {
    const d = text.match(new RegExp("^!define\\s+" + name + "\\s+`([\\s\\S]*?)`\\s*$", "m"));
    ok(d, "缺少 !define " + name);
    if (d) s = s.split("${" + name + "}").join(d[1]);
  }
  return s;
}
function macroBody(name) {
  // \b：customInstall 不得误配 customInstallMode（2026-09-05 加模式页宏时踩到）
  const m = text.match(new RegExp("!macro\\s+" + name + "\\b[\\s\\S]*?!macroend"));
  ok(m, "缺少 !macro " + name);
  return m ? expandDefines(m[0]) : "";
}
const header = macroBody("customHeader");
const welcome = macroBody("customUnWelcomePage");
const uninst = macroBody("customUnInstall");

// customUnWelcomePage 是「替换」语义——必须把默认欢迎页重新插回来
ok(welcome.includes("MUI_UNPAGE_WELCOME"), "customUnWelcomePage 必须重新插入 MUI_UNPAGE_WELCOME（否则默认欢迎页被顶没）");
ok(welcome.includes("UninstPage custom"), "customUnWelcomePage 必须注册自定义数据处置页");

// ---- C1: isUpdated 硬守卫在一切 RMDir 之前 -----------------------------------
const guardAt = uninst.indexOf("${isUpdated}");
const firstRm = uninst.indexOf("RMDir");
ok(guardAt >= 0, "customUnInstall 缺少 ${isUpdated} 升级守卫");
ok(firstRm > guardAt, "isUpdated 守卫必须出现在第一个 RMDir 之前（升级链绝不删数据）");

// ---- C2: 显式 CLI 通道 --------------------------------------------------------
ok(uninst.includes('"--delete-app-data"'), "customUnInstall 缺少 --delete-app-data 参数解析（静默运维清数据通道）");

// ---- 决策变量禁用寄存器（2026-08-21 实弹根因） ---------------------------------
// electron-builder 生成的 flag 测试宏（isUpdated/isForceRun/...）展开成
// `${StdUtils.TestParameter} $R9 "<flag>"` —— 会覆写 $R9。首版把 wipe 决策存
// $R9，`${If} ${isUpdated}` 一展开就把它踩成 "false"，整个增强 wipe 层成了
// 死代码（模板内置分支恰好删掉 Roaming 两目录，掩盖了死亡：updater 缓存幸存、
// 零 wipe log、零 RunOnce、卸载仅 8s）。决策必须走自有 Var，永不走寄存器。
ok(!/StrCpy \$R9\b/.test(uninst), "customUnInstall 禁止写 $R9（electron-builder flag 测试宏的暂存器，${If} ${isUpdated} 会覆写它）");
ok(welcome.includes("Var cxWipeGo"), "缺少 Var cxWipeGo 声明（wipe 决策自有变量）");
ok(uninst.includes('${If} $cxWipeGo == "1"'), "wipe 执行闸必须读 $cxWipeGo（自有决策变量）");

// ---- C3: 删除目标只许宏路径（删除行零 CJK 字面量） ----------------------------
// 真删经 cxRmDirRetry 宏（短锁重试：Defender 扫描刚拷入的 installer.exe 时
// 首删撞锁的实测教训），宏体内 RMDir 用形参 TARGET_DIR，调用点必须 APP_* 宏。
const cjk = /[\u4e00-\u9fff]/;
for (const line of text.split(/\r?\n/)) {
  const isDeleteLine = /^\s*RMDir/i.test(line) || /^\s*!insertmacro\s+cxRmDirRetry/.test(line);
  if (isDeleteLine) {
    ok(!cjk.test(line), "删除行出现 CJK 字面路径（必须用 ${APP_FILENAME} 族宏）: " + line.trim());
    ok(
      /\$\{APP_(FILENAME|PRODUCT_FILENAME|PACKAGE_NAME)\}/.test(line) || /\$\{TARGET_DIR\}/.test(line),
      "删除行必须由 APP_* 宏（或重试宏形参）推导: " + line.trim()
    );
  }
}
// 重试宏本体：预算式循环（覆盖 AV 扫描类短锁，实测 Defender 扫刚拷入的 500MB
// installer.exe 时 2s 单次重试盖不住）+ /REBOOTOK 兜底（长锁走
// PendingFileRenameOperations 重启自动清，Windows 正统路径）
const retry = macroBody("cxRmDirRetry");
ok(/IntOp[\s\S]*Sleep[\s\S]*Goto/.test(retry), "cxRmDirRetry 必须是预算式重试循环（计数+Sleep+Goto）");
ok(retry.includes("/REBOOTOK"), "cxRmDirRetry 必须带 /REBOOTOK 兜底（提升态卸载时重启自动清）");
// per-user 卸载不提升，REBOOTOK 写 HKLM 会静默无效（2026-08-21 实测 PFRO 零登记）
// —— HKCU RunOnce 才是普通用户真正会触发的兜底，两层缺一不可
ok(/WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce"/.test(retry), "cxRmDirRetry 必须带 HKCU RunOnce 兜底（per-user 卸载无提升，REBOOTOK 写不进 HKLM）");
// NSIS 不支持反斜杠续行——出现「空格+反斜杠+行尾」多半是把长行拆行的手滑
ok(!/ \\\r?\n/.test(retry), "cxRmDirRetry 内出现疑似续行反斜杠（NSIS 长行必须单行写）");
// 至少覆盖 userData 与 updater 缓存两类目标
ok(/!insertmacro cxRmDirRetry "\$APPDATA\\\$\{APP_FILENAME\}"/.test(uninst), "缺少 userData 主目录删除");
ok(/-updater"/.test(uninst), "缺少 electron-updater 缓存目录删除（wipe_chatx_data_node.ps1 口径）");

// ---- C4: 进程收割按路径 + 自杀防护 -------------------------------------------
ok(uninst.includes("!insertmacro cxReapFamily"), "customUnInstall 缺少进程收割（后端/sidecar 锁住 db 时 RMDir 会静默残留）");
ok(!/taskkill[^\n]*智/.test(text), "禁止按 CJK 进程名杀进程");

// ---- C5: 删后校验 + 如实报告 --------------------------------------------------
ok(uninst.includes("IfFileExists"), "customUnInstall 缺少删后校验（IfFileExists）");
ok(uninst.includes("cxWipeLeftover"), "customUnInstall 缺少残留如实报告（cxWipeLeftover）");
ok(uninst.includes("cxWipeDone"), "customUnInstall 缺少成功确认（cxWipeDone）");

// ---- 用户可见路径 = 真实打包数据目录（2026-08-21 深审实锤三修） -----------------
// electron-builder 对非 ASCII productName 会把 APP_FILENAME 回落成包名
// （getWindowsInstallationDirName 的 ASCII-safe 判定），真实 userData 是
// $APPDATA\${APP_PRODUCT_FILENAME}（CJK 产品名目录）。用 APP_FILENAME 展示/打开
// = 错误路径 + 用户机上目录不存在 → 「打开数据文件夹」链接静默哑掉；
// 删后校验漏它 = 主数据目录锁死残留时谎报「已彻底清除」（违反 C5）。
ok(
  text.includes('!define CX_USERDATA_DIR "$APPDATA\\${APP_PRODUCT_FILENAME}"'),
  "缺少 CX_USERDATA_DIR 定义（必须优先 APP_PRODUCT_FILENAME——真实 CJK 数据目录）"
);
ok(welcome.includes("${CX_USERDATA_DIR}"), "数据处置页必须经 CX_USERDATA_DIR 展示/打开数据目录");
ok(
  !welcome.includes("$APPDATA\\${APP_FILENAME}"),
  "数据处置页禁止裸引用 $APPDATA\\${APP_FILENAME}（打包态那是 DEV 目录，对用户是错误路径）"
);
ok(
  uninst.includes('IfFileExists "$APPDATA\\${APP_PRODUCT_FILENAME}\\*.*"'),
  "删后校验缺少 APP_PRODUCT_FILENAME 主数据目录（漏了=锁死残留时谎报已清除）"
);

// ---- C6: 安装侧全家族收割（B57「升级风暴」的安装器半件，2026-08-24） -----------
// 库存 _CHECK_APP_RUNNING 只管主 exe（壳+ELECTRON_RUN_AS_NODE 边车同镜像名），
// PyInstaller backend.exe 不在覆盖面——旧后端孤儿抱着 18799 与 pyrogram 会话文件
// 不放，新后端一起来就撞 AuthKeyDuplicated → 全账号被强制注销（升级必重登根因）。
const checkRunning = macroBody("customCheckAppRunning");
// 2026-09-20 二轮实弹：库存 _CHECK_APP_RUNNING 的总预算只有 ~6.3s，且决定性的那次
// 复查紧贴 taskkill /f 之后、零沉降时间；而 main.js 的 before-quit 故意
// preventDefault + 等 stopAndWait(8000)+sidecars.stopAll（B57 要求），按设计就要 8s+。
// 于是「按设计正常退出」的应用必输这场没人写下来的竞速 →「智聊 无法关闭」弹窗（静默
// 态更糟：appCannotBeClosed 带 /SD IDCANCEL＝直接 Quit，升级中止）。所以整段关闭由
// 我们自己按「先请、再等实证、后强杀、再复验」的顺序接管，绝不再插库存那段。
ok(
  !checkRunning.includes("_CHECK_APP_RUNNING"),
  "customCheckAppRunning 禁止插入库存 _CHECK_APP_RUNNING（它 ~6.3s 的预算短于本应用 8s+ 的 before-quit 关停，必弹「无法关闭」；静默态 /SD IDCANCEL 直接中止升级）"
);
ok(
  checkRunning.indexOf("cxCloseAppNicely") >= 0 &&
    checkRunning.indexOf("cxCloseAppNicely") < checkRunning.indexOf("cxReapFamily"),
  "必须先优雅关闭再强杀（WM_CLOSE 让 before-quit 干净带走后端，B57：半死的后端抢会话=全账号重登）"
);
ok(
  !/\bQuit\b/.test(checkRunning) && /MessageBox[^\n]*\/SD /.test(checkRunning),
  "关闭失败禁止 Quit、弹窗必须带 /SD（覆盖安装远好过让升级死在这里；无 /SD 会挂死静默安装）"
);
const nicely = macroBody("cxCloseAppNicely");
ok(
  nicely.includes("CloseMainWindow"),
  "cxCloseAppNicely 必须用 CloseMainWindow（WM_CLOSE 才会触发 before-quit 的后端清退；强杀跳过它=孤儿后端）"
);
ok(
  /while/.test(nicely) && /exit 0/.test(nicely) && /exit 1/.test(nicely),
  "cxCloseAppNicely 必须轮询等到家族真的清空（固定 Sleep 就是库存那段的原罪）"
);
ok(
  /!insertmacro cxCloseAppNicely\s+(\d+)/.test(checkRunning) &&
    Number(/!insertmacro cxCloseAppNicely\s+(\d+)/.exec(checkRunning)[1]) >= 12,
  "优雅关闭的等待预算必须 >= 12s（stopAndWait 自己就是 8s，再加 sidecars.stopAll；预算短于关停耗时=复刻库存 bug）"
);
const reap = macroBody("cxReapFamily");
ok(
  reap.includes("'$INSTDIR\\*'"),
  "cxReapFamily 必须按 $INSTDIR 匹配（只认 ${APP_PACKAGE_NAME} 会漏掉用户在向导里自选的安装目录）"
);
ok(
  reap.includes("chatx_install_reap.log"),
  "cxReapFamily 必须把幸存进程写进日志（现场再炸时要有证据，不能再靠猜）"
);
ok(reap.includes("Stop-Process"), "cxReapFamily 缺少 Stop-Process 收割");
ok(reap.includes("-notlike '*Uninstall*'"), "cxReapFamily 必须排除 '*Uninstall*'（C4 教训：卸载器 _?= 原地运行会杀自己）");
ok(reap.includes("-notlike '*-updater*'"), "cxReapFamily 必须排除 '*-updater*'（自动更新的安装器本体在 updater 缓存目录里运行，杀了=自杀）");
ok(reap.includes("'*\\${APP_PACKAGE_NAME}\\*'"), "cxReapFamily 路径必须反斜杠界定 ${APP_PACKAGE_NAME}（裸 '*name*' 会误匹配 -updater 兄弟目录）");
// 2026-09-20 实弹根因：Get-Process().Path 走 Process.MainModule，需要目标进程的
// PROCESS_VM_READ；从 per-user 安装器起的 PowerShell 里 342 个进程只有 18 个读得到
// .Path，谓词命中 0 —— 整张 C6 收割网从写下那天起就是空转。必须走 WMI 的
// ExecutablePath（内核侧取值，不需要句柄）。
ok(
  reap.includes("Win32_Process") && reap.includes("ExecutablePath"),
  "cxReapFamily 必须用 Win32_Process.ExecutablePath 匹配（Get-Process().Path 在安装器上下文读不到，收割会静默空转）"
);
// 只看可执行行：本条教训写在注释里，别让门禁把自己的血泪注释判成违规
const codeOnly = text.split(/\r?\n/).filter((l) => !/^\s*;/.test(l)).join("\n");
ok(!/Get-Process\s*\|/.test(codeOnly), "禁止用 `Get-Process |` 按 .Path 筛进程（安装器上下文里 .Path 基本全空）");
ok(
  reap.includes("$SYSDIR\\WindowsPowerShell\\v1.0\\powershell.exe"),
  "cxReapFamily 必须用 PowerShell 绝对路径（powershell.exe 不在 System32 下，裸名字只能靠 PATH 解析，PATH 一坏收割就静默失效）"
);
ok(
  /exit 0/.test(reap) && /exit 1/.test(reap) && /while/.test(reap),
  "cxReapFamily 必须杀完复验并循环到真死（固定 Sleep 不是死亡证据，下一步模板就要逐个改名 $INSTDIR 的文件）"
);

// ---- C8: 旧版卸载失败必须可恢复，且绝不阻塞静默通道（2026-09-20） -------------
// 模板 handleUninstallResult 的收尾是「无 /SD 的 MessageBox + SetErrorLevel 2 +
// Quit」：交互态把用户顶在「升级失败」上，静默态（/S = 运维推机与热更新通道）
// 直接永久挂起（实测挂了 18 分钟）。定义 customUnInstallCheck 接管这段。
const unCheck = macroBody("cxUnInstallCheckBody");
ok(unCheck.length > 0, "缺少 cxUnInstallCheckBody（旧版卸载失败的接管逻辑）");
ok(text.includes("!macro customUnInstallCheck"), "必须定义 customUnInstallCheck 接管模板的卸载失败收尾");
ok(text.includes("!macro customUnInstallCheckCurrentUser"), "必须同时接管 HKEY_CURRENT_USER 侧（installMode=all 走这条）");
ok(unCheck.includes("!insertmacro cxReapFamily"), "卸载失败接管必须先重新收割进程（失败的唯一成因就是文件还被占用）");
ok(unCheck.includes("Call uninstallOldVersion"), "卸载失败接管必须再给一轮卸载重试");
ok(!/\bQuit\b/.test(unCheck), "卸载失败接管禁止 Quit（就地覆盖安装远好过升级直接死掉）");
ok(/MessageBox[^\n]*\/SD /.test(unCheck), "卸载失败接管的弹窗必须带 /SD 默认值（无 /SD 的 MessageBox 会把静默安装永久挂死）");
// 定义 customCheckAppRunning 会让模板跳过它自己的 getProcessInfo/Var pid（
// allowOnlyOneInstallerInstance.nsh 的 ifmacrondef 守卫）。1.0.93 起我们不再插库存
// 那段，$pid 也就没人引用了——留着 `Var pid` 会被 makensis -WX 判成未引用变量而直接
// 编译失败，所以两者必须一起消失。
ok(
  !text.includes('!include "getProcessInfo.nsh"') && !/^Var pid\r?$/m.test(text),
  "不再插入库存 _CHECK_APP_RUNNING 后，getProcessInfo.nsh 与 Var pid 必须一并删掉（未引用变量在 -WX 下是硬编译失败）"
);

// ---- C7: 安装侧「保留/清空数据」页 + 执行闸 -----------------------------------
// 2026-09-05 起数据页注册在 customWelcomePage（欢迎页之后、须知页之前），不再挂
// customPageAfterChangeDir：NSIS 只给「物理上紧邻 instfiles 的那一页」标「安装」按钮，
// 数据页排在目录页之后时，全新安装（数据页运行时跳过）目录页显示「下一步」却直接开装。
const insPage = macroBody("customWelcomePage");
ok(insPage.includes("Page custom cxInsDataPageCreate"), "customWelcomePage 必须注册安装侧数据处置页（紧随欢迎页）");
ok(
  insPage.indexOf("MUI_PAGE_WELCOME") < insPage.indexOf("Page custom cxInsDataPageCreate") &&
    insPage.indexOf("Page custom cxInsDataPageCreate") < insPage.indexOf('MUI_PAGE_HEADER_TEXT "$(cxNoticeTitle)"'),
  "安装页序必须是 欢迎 → 数据处置 → 须知（须知页设置定义在数据页注册之后，MUI 按下一页消费）"
);
ok(!macroBody("customPageAfterChangeDir").includes("Page custom"), "customPageAfterChangeDir 不得再注册页面（目录页必须是 instfiles 前最后一页，否则「安装」按钮消失）");
ok(insPage.includes("Var cxInsWipeGo"), "缺少 Var cxInsWipeGo（安装侧 wipe 决策自有变量，$R9 教训同 C1）");
ok(/\$\{If\} \$\{isUpdated\}\s*\r?\n\s*Abort/.test(insPage), "cxInsDataPageCreate 必须在 isUpdated（自动更新链）直接跳过页面");
ok(insPage.includes("${NSD_SetState} $cxInsRadioKeep ${BST_CHECKED}"), "安装侧数据页默认必须选中「保留数据」");
ok(insPage.includes("$(cxNeedConfirm)"), "安装侧 wipe 必须勾选不可恢复确认才放行");
ok(insPage.includes("CX_DATA_DIR_PKG"), "存在性检查/展示必须用 CX_DATA_DIR_PKG（实测 userData=%APPDATA%\\<包名>，只查 CJK 产品目录页面永远不出现）");

const insMac = macroBody("customInstall");
const insGuardAt = insMac.indexOf("${isUpdated}");
const insFirstRm = insMac.search(/RMDir|!insertmacro\s+cxRmDirRetry/);
ok(insGuardAt >= 0, "customInstall 缺少 ${isUpdated} 归零守卫");
ok(insFirstRm > insGuardAt, "customInstall 的 isUpdated 归零必须在第一个删除动作之前（自动更新绝不清数据，C1 镜像）");
ok(insMac.includes('"--wipe-app-data"'), "customInstall 缺少 --wipe-app-data 显式参数通道（静默清空安装，镜像卸载器 --delete-app-data）");
ok(!/StrCpy \$R9\b/.test(insMac), "customInstall 禁止写 $R9（flag 测试宏的暂存器）");
ok(insMac.includes('${If} $cxInsWipeGo == "1"'), "安装侧 wipe 执行闸必须读 $cxInsWipeGo（自有决策变量）");
ok(insMac.includes("IfFileExists"), "customInstall 缺少删后校验（IfFileExists）");
ok(insMac.includes("cxWipeLeftover"), "customInstall 缺少残留如实报告（cxWipeLeftover）");

// ---- LangString 双语齐平 ------------------------------------------------------
function langKeys(langMacro) {
  const keys = new Set();
  const re = new RegExp("LangString\\s+(\\w+)\\s+\\$\\{LANG_" + langMacro + "\\}", "g");
  let m;
  while ((m = re.exec(header))) keys.add(m[1]);
  return keys;
}
const en = langKeys("ENGLISH");
const zh = langKeys("SIMPCHINESE");
ok(en.size > 0, "customHeader 无英文 LangString");
for (const k of en) ok(zh.has(k), "LangString 缺中文: " + k);
for (const k of zh) ok(en.has(k), "LangString 缺英文: " + k);

// LangString 必须待在 customHeader 内（顶层写会在语言表加载前编译失败）
const outsideHeader = text.replace(header, "");
ok(!/^\s*LangString/m.test(outsideHeader), "LangString 只能定义在 customHeader 宏内（include 被前置到语言表加载之前）");

// Var 声明必须在宏体内：include 被编进安装器+卸载器两个单元，宏只在其中一个单元
// 展开——顶层 Var 在另一单元「声明未引用」，makensis -WX 把该警告变成硬失败
//（2026-08-21 首次打包实锤，warning 6001 treated as error）。
// 唯一豁免 = `Var pid`：customCheckAppRunning 在两个单元的 CHECK_APP_RUNNING 里
// 都展开（installSection + un.checkAppRunning），$pid 两边都被引用，顶层反而是
// 唯一正确位置（塞进宏体会在第二次插入时重复声明）。
const outsideMacros = text
  .replace(/!macro[\s\S]*?!macroend/g, "")
  .replace(/^Var pid\r?$/m, "");
ok(!/^\s*Var\s/m.test(outsideMacros), "Var 声明必须在宏体内（顶层 Var 在另一编译单元未引用，-WX 下打包必炸；唯一豁免=Var pid）");

// ---- 品牌/界面 chrome（2026-09-05 安装器视觉收口） -----------------------------
// 2026-09-05 之前安装器零品牌资产：electron-builder 找不到 build/installerSidebar.bmp
// 会**静默**回落到 NSIS 自带 nsis3-metro.bmp（Windows 蓝底 + NSIS 自己的 logo），
// 没有任何报警——完成页/卸载欢迎页给第三方打了几十个版本的广告。这里把资产
// 存在性、格式（24 位 BMP，STM_SETIMAGE 下 32 位会发黑）、尺寸（MUI 标准 x2，
// 配 ManifestDPIAware）钉死，缺一样安装包拒绝出生。
function bmpInfo(p) {
  const b = fs.readFileSync(p);
  return {
    magic: b.toString("ascii", 0, 2),
    offBits: b.readUInt32LE(10),
    width: b.readInt32LE(18),
    height: b.readInt32LE(22),
    bpp: b.readUInt16LE(28),
    // 首个像素（BMP 自底向上 → 文件里第一个像素是左下角；BGR 顺序）→ "RRGGBB"
    firstPixel: (() => {
      const o = b.readUInt32LE(10);
      return [b[o + 2], b[o + 1], b[o]].map((v) => v.toString(16).padStart(2, "0")).join("").toUpperCase();
    })(),
  };
}
ok(nsis.installerSidebar === "build/installerSidebar.bmp", "build.nsis.installerSidebar 必须显式指向 build/installerSidebar.bmp（隐式约定缺文件会静默回落 NSIS 自带图）");
ok(nsis.installerHeader === "build/installerHeader.bmp", "build.nsis.installerHeader 必须显式指向 build/installerHeader.bmp");
// 尺寸 = YaHei UI 9pt 下 MUI 控件的真实像素（191x410 / 175x74 @96dpi，2026-09-05 实机
// EnumChildWindows 量得）x2；不是 NSIS 文档的 164x314 / 150x57——那是 MS Shell Dlg 8pt
// 的几何，换了字体就按新比例出图，否则 ChatX 图标被横向挤压 ~11%
for (const [key, w, h] of [["installerSidebar", 382, 820], ["installerHeader", 350, 148]]) {
  const p = path.join(appDir, nsis[key]);
  ok(fs.existsSync(p), `缺 ${nsis[key]}（重跑 brand-assets/build_installer_art.py）`);
  const info = bmpInfo(p);
  ok(info.magic === "BM", `${nsis[key]} 不是 BMP`);
  ok(info.bpp === 24, `${nsis[key]} 必须是 24 位 BMP（当前 ${info.bpp} 位；带 alpha 的位图在 MUI 里发黑）`);
  ok(info.width === w && info.height === h, `${nsis[key]} 尺寸必须 ${w}x${h}（MUI 标准尺寸 x2；当前 ${info.width}x${info.height}）`);
  if (key === "installerHeader") {
    // 页眉位图底色必须 == MUI_BGCOLOR，否则页眉带右侧出现色块拼缝
    const m = /!define MUI_BGCOLOR\s+"?([0-9A-Fa-f]{6})"?/.exec(text);
    ok(m, "缺 !define MUI_BGCOLOR（页眉/欢迎/完成页底色）");
    ok(info.firstPixel === m[1].toUpperCase(), `installerHeader.bmp 底色 ${info.firstPixel} 必须与 MUI_BGCOLOR ${m[1].toUpperCase()} 一致（否则页眉拼缝）`);
  }
}
// 内测角标变体（round 2）：非 clean 形态换用 -internal 位图 + 品牌栏后缀；flavor.nsh 由
// write-build-info.js 产出（gitignore），缺文件按 internal 处理——失败模式必须是「多个角标」
for (const [name, w, h] of [["installerSidebar-internal.bmp", 382, 820], ["installerHeader-internal.bmp", 350, 148]]) {
  const p = path.join(appDir, "build", name);
  ok(fs.existsSync(p), `缺 build/${name}（重跑 brand-assets/build_installer_art.py）`);
  const info = bmpInfo(p);
  ok(info.bpp === 24 && info.width === w && info.height === h, `build/${name} 必须 24 位 ${w}x${h}（当前 ${info.bpp} 位 ${info.width}x${info.height}）`);
  if (name.startsWith("installerHeader")) ok(info.firstPixel === "FFFFFF", `build/${name} 底色必须与 MUI_BGCOLOR 一致`);
}
ok(/^!include \/NONFATAL "flavor\.nsh"\r?$/m.test(outsideMacros), "缺 !include /NONFATAL flavor.nsh（形态事实来自 write-build-info.js）");
ok(/!ifndef CX_FLAVOR\r?\n\s*!define CX_FLAVOR "internal"/.test(text), "flavor.nsh 缺失时必须默认 internal（角标常亮，内测包不得冒充干净包）");
ok(/!if "\$\{CX_FLAVOR\}" != "clean"[\s\S]*?MUI_WELCOMEFINISHPAGE_BITMAP "\$\{BUILD_RESOURCES_DIR\}\\installerSidebar-internal\.bmp"[\s\S]*?MUI_UNWELCOMEFINISHPAGE_BITMAP "\$\{BUILD_RESOURCES_DIR\}\\installerSidebar-internal\.bmp"[\s\S]*?MUI_HEADERIMAGE_BITMAP "\$\{BUILD_RESOURCES_DIR\}\\installerHeader-internal\.bmp"/.test(text), "非 clean 形态必须把三张位图（安装/卸载侧栏 + 页眉）都换成 -internal 变体");
ok(/STR:\$\(cxBranding\)\$\{CX_BRAND_SUFFIX\}/.test(macroBody("cxSetBranding")), "品牌栏必须带 ${CX_BRAND_SUFFIX}（内测后缀）");
ok(fs.readFileSync(path.join(appDir, "build", "write-build-info.js"), "utf8").includes('"flavor.nsh"'), "build/write-build-info.js 必须产出 flavor.nsh");
ok(/^build\/flavor\.nsh\r?$/m.test(fs.readFileSync(path.join(appDir, ".gitignore"), "utf8")), "desktop/.gitignore 缺 build/flavor.nsh（生成物不得入库）");
ok(/^ManifestDPIAware true\r?$/m.test(outsideMacros), "缺 ManifestDPIAware true（坐席 4K@200% 下整个安装器被系统位图拉伸发虚）");
// 窗口标题随向导语言（英文实机截图曾显示「智聊 Setup」）
ok(/^Caption "\$\(cxCaption\)"\r?$/m.test(outsideMacros) && /^UninstallCaption "\$\(cxUnCaption\)"\r?$/m.test(outsideMacros), "缺 Caption/UninstallCaption LangString（英文向导标题会是「智聊 Setup」）");
// 英文长句上限（2026-09-05 英文实机截图：keep-detail 三行溢出 18u 标签被截）：
// 数据页说明行 2 行 ≈ 150 字符，欢迎页单条 bullet ≈ 50 字符
{
  const enStr = (key) => { const m = new RegExp('LangString\\s+' + key + '\\s+\\$\\{LANG_ENGLISH\\}\\s+"((?:[^"\\\\]|\\\\.|\\$\\")*)"').exec(header); return m ? m[1] : ""; };
  ok(enStr("cxKeepDetail").length > 0 && enStr("cxKeepDetail").length <= 150, "cxKeepDetail 英文超过 150 字符会在 18u（两行）标签里被截（当前 " + enStr("cxKeepDetail").length + "）");
  // 完成页正文不许再写官网地址：页面底部的 MUI_FINISHPAGE_LINK 已经是它（隔离用户实装截图：同一行出现两次）
  for (const key of ["cxFinText", "cxUnFinText", "cxUnFinWipedTail", "cxUnFinLeftTail", "cxInsFinWiped", "cxInsFinLeft"]) {
    for (const m of header.matchAll(new RegExp('LangString\\s+' + key + '\\s+\\$\\{LANG_\\w+\\}\\s+"([^"]*)"', "g"))) {
      ok(!/bd2026\.cc/.test(m[1]), key + " 正文含官网地址，与完成页底部链接重复: " + m[1].slice(0, 40));
    }
  }
  for (const line of enStr("cxWelText").split("$\\r$\\n")) {
    // 实测：53 字符一行放得下，58 字符折行（195u 宽 YaHei UI 9pt）
    if (line.startsWith("-  ")) ok(line.length <= 54, "欢迎页英文 bullet 超过一行会折行错位: " + line);
  }
}
// SimpChinese.nlf 硬编码宋体 9pt：不在 customHeader（MUI_LANGUAGE 之后唯一钩子）覆盖，
// 中文安装器全程宋体（2026-09-05 首次实机截图实锤）
ok(/SetFont \/LANG=\$\{LANG_SIMPCHINESE\} "Microsoft YaHei UI" 9/.test(header), "customHeader 缺 SetFont /LANG=${LANG_SIMPCHINESE} \"Microsoft YaHei UI\" 9（否则中文界面回落 NLF 的宋体）");
// 英文也用同一字体：对话框单位随字体变，MUI 把位图拉到控件大小——两种语言同字体 =
// 控件长宽比唯一，位图按该比例出图才不会被挤压（YaHei UI 的拉丁字形本就是 Segoe UI）
ok(/SetFont \/LANG=\$\{LANG_ENGLISH\} "Microsoft YaHei UI" 9/.test(header), "customHeader 缺 SetFont /LANG=${LANG_ENGLISH} \"Microsoft YaHei UI\" 9（两种语言必须同字体，位图长宽比才唯一）");
ok(/^!define MUI_ABORTWARNING\r?$/m.test(outsideMacros), "缺 MUI_ABORTWARNING（点 × 直接退出无确认）");
ok(/^!define MUI_UNABORTWARNING\r?$/m.test(outsideMacros), "缺 MUI_UNABORTWARNING（卸载器点 × 直接退出无确认）");
// 颜色纪律：SetCtlColors 只许引用 CX_C_* 令牌常量。裸色值 = 绕开令牌，且 NSIS 按 HTML
// RRGGBB 顺序解析——首版把 0x1F1FBF 当红色写，实际渲染成深蓝（2026-09-05 读码实锤）。
const colorDefs = {};
for (const m of text.matchAll(/^!define (CX_C_\w+)\s+"([0-9A-Fa-f]{6})"\r?$/gm)) colorDefs[m[1]] = m[2];
ok(Object.keys(colorDefs).length >= 3, "缺 CX_C_* 颜色常量（至少 TEXT2/LINK/WARN 三档）");
for (const line of text.split(/\r?\n/)) {
  const m = /^\s*SetCtlColors\s+\$\w+\s+(\S+)/.exec(line);
  if (!m) continue;
  const tok = m[1];
  const ref = /^\$\{(CX_C_\w+)\}$/.exec(tok);
  ok(ref && colorDefs[ref[1]], "SetCtlColors 只许用 ${CX_C_*} 令牌常量（裸色值绕开品牌令牌）: " + line.trim());
}
// 链接纪律：每个 NSD_CreateLink 六行内必须绑定 NSD_OnClick（安装侧帮助链接曾是死链）
{
  const lines = text.split(/\r?\n/);
  lines.forEach((ln, i) => {
    if (!/\$\{NSD_CreateLink\}/.test(ln)) return;
    const win = lines.slice(i + 1, i + 7).join("\n");
    ok(/\$\{NSD_OnClick\}/.test(win), "NSD_CreateLink 之后 6 行内没有 NSD_OnClick（死链）: " + ln.trim());
  });
}
// 须知页：按语言的 RTF（build/license_<lang>.rtf）由 write-installer-notice.js 从 .txt 源
// 生成并入库。build.nsis.license 必须留空——一旦设了单文件，electron-builder 就不再
// 收集按语言文件，中文用户又会看到中英对照半屏。RTF 内嵌源文本 sha1，改了源忘了重跑
// 生成脚本 → 这里红。
ok(nsis.license == null, "build.nsis.license 必须留空（设了单文件会禁用按语言的 license_<lang>.rtf 收集）");
const notice = require(path.join(appDir, "build", "write-installer-notice.js"));
for (const [srcName, outName] of notice.TARGETS) {
  const srcPath = path.join(appDir, "build", srcName);
  const outPath = path.join(appDir, "build", outName);
  ok(fs.existsSync(srcPath), `缺须知页源文件 build/${srcName}`);
  ok(fs.existsSync(outPath), `缺 build/${outName}（运行 node build/write-installer-notice.js）`);
  const rtf = fs.readFileSync(outPath, "latin1");
  ok(rtf.startsWith("{\\rtf1"), `build/${outName} 不是 RTF`);
  ok(!/[\x80-\xff]/.test(rtf), `build/${outName} 含原始高字节（非 ASCII 必须 \\uN? 转义，否则 RichEdit 按代码页解码乱码）`);
  const m = /\{\\\*\\cxsrc ([0-9a-f]{40})\}/.exec(rtf);
  const srcText = fs.readFileSync(srcPath, "utf8").replace(/^\uFEFF/, "");
  ok(m && m[1] === notice.sourceHash(srcText), `build/${outName} 已过期：${srcName} 改了但没重跑 node build/write-installer-notice.js`);
}
ok(!fs.existsSync(path.join(appDir, "build", "installer-notice.txt")), "旧的单文件 build/installer-notice.txt 应已删除（须知页改为按语言 RTF）");
// 欢迎页 / 完成页 / 模式页 / 进度状态行（2026-09-05 流程收口）
ok(/!macro\s+customWelcomePage[\s\S]*?MUI_PAGE_WELCOME[\s\S]*?!macroend/.test(text), "缺 customWelcomePage（electron-builder 助手式默认无欢迎页，首屏会是许可页）");
ok(/!macro\s+customWelcomePage[\s\S]*?skipPageIfUpdated[\s\S]*?MUI_PAGE_WELCOME/.test(text), "customWelcomePage 必须先插 skipPageIfUpdated（非静默自动更新不该多一页）");
ok(/!macro\s+customInstallMode[\s\S]*?isForceCurrentInstall[\s\S]*?!macroend/.test(text), "缺 customInstallMode（隐藏「为哪位用户安装」页，按用户安装是产品设定）");
ok(/!macro\s+customInstallMode[\s\S]*?hasPerMachineInstallation[\s\S]*?!macroend/.test(text), "customInstallMode 必须给旧的全机安装留例外（否则会在旁边再装一份）");
ok(/!ifndef BUILD_UNINSTALLER[\s\S]*?MUI_FINISHPAGE_TITLE[\s\S]*?!else[\s\S]*?MUI_FINISHPAGE_TITLE[\s\S]*?!endif/.test(text), "MUI_FINISHPAGE_* 必须按 BUILD_UNINSTALLER 分支（卸载完成页与安装完成页消费同一组设置）");
// 清数据结果走完成页文案而非弹窗（round 2）：完成页文本是变量，customInit/customUnInit 播种
// 默认文案，wipe 分支改写；卸载侧 REBOOTOK 残留会触发 MUI 重启变体页，同一变量也要接上
ok(/!macro\s+customUnInit\b[\s\S]*?StrCpy \$cxUnFinMsg "\$\(cxUnFinText\)"[\s\S]*?!macroend/.test(text), "缺 customUnInit 播种 $cxUnFinMsg 默认文案（否则完成页空白）");
ok(/!macro\s+customInit\b[\s\S]*?StrCpy \$cxInsFinMsg "\$\(cxFinText\)"[\s\S]*?!macroend/.test(text), "缺 customInit 播种 $cxInsFinMsg 默认文案（否则完成页空白）");
ok(welcome.includes("Var cxUnFinMsg") && welcome.includes("Var cxUnFinTitle"), "customUnWelcomePage 缺 Var cxUnFinMsg/cxUnFinTitle（卸载完成页变量须在宏内声明）");
ok(insPage.includes("Var cxInsFinMsg"), "customWelcomePage 缺 Var cxInsFinMsg");
ok(/!else[\s\S]*?!define MUI_FINISHPAGE_TEXT "\$cxUnFinMsg"[\s\S]*?!define MUI_FINISHPAGE_TEXT_REBOOT "\$cxUnFinMsg"[\s\S]*?MUI_FINISHPAGE_REBOOTLATER_DEFAULT[\s\S]*?!endif/.test(text), "卸载完成页 TEXT 与 TEXT_REBOOT 都必须绑 $cxUnFinMsg 且默认「稍后重启」（残留走 /REBOOTOK 会切到重启变体页）");
ok(/!define MUI_FINISHPAGE_TEXT "\$cxInsFinMsg"/.test(text), "安装完成页 TEXT 必须绑 $cxInsFinMsg");
ok(!/MessageBox/.test(uninst), "customUnInstall 不得再弹 MessageBox（清数据结果写进完成页；C5 如实报告仍由 cxWipeLeftover/cxWipeDone 文案承载）");
ok(/StrCpy \$cxUnFinMsg "\$\(cxWipeLeftover\)\$\\r\$\\n\$cxLeft/.test(uninst), "残留路径必须写进 $cxUnFinMsg（如实点名）");
ok(insMac.includes('StrCpy $cxInsFinMsg "$(cxInsFinWiped)"') && insMac.includes('StrCpy $cxInsFinMsg "$(cxInsFinLeft)"'), "customInstall 两个 wipe 结果分支都必须改写 $cxInsFinMsg");
ok(macroBody("customPageAfterChangeDir").includes("MUI_PAGE_CUSTOMFUNCTION_SHOW cxInstFilesShow"), "缺进度页状态行（模板 SetDetailsPrint none 后进度页全程无字；必须挂在 customPageAfterChangeDir——唯一紧邻 MUI_PAGE_INSTFILES 的钩子）");
// 两页同一套版式：粗体选项标题 / 分隔线 / 路径省略号（视觉层级 + 长路径不截尾）
for (const [name, body] of [["un.cxDataPageCreate", welcome], ["cxInsDataPageCreate", insPage]]) {
  ok(/CreateFont\s+\$\w+\s+"\$\(\^Font\)"\s+"\$\(\^FontSize\)"\s+"700"/.test(body), name + " 缺粗体字体（两个选项标题用对话框字体 700 字重）");
  ok(body.includes("${NSD_CreateHLine}"), name + " 缺选项分隔线");
  ok(body.includes("${SS_PATHELLIPSIS}"), name + " 数据路径行缺 SS_PATHELLIPSIS（长路径被截尾）");
}
ok(!/DetailPrint\s+"ChatX:/.test(text), "DetailPrint 不得再用英文硬编码（卸载器进度页可见，双语 UI 里必须走 LangString）");
// 底部品牌栏（控件 1028）运行时改写：两个编译单元各一份函数，缺一边 = 该单元未引用函数 → -WX 炸
// 品牌栏是两个控件：1028 可见文字 + 1256 盖在刻线 1035 上、按自身文字宽度画不透明底的遮罩。
// makensis 探针实测（2026-09-05）：只写 1028 = 文字被线贯穿；只写 1256 = 看不出变化；两个都写才对。
const brandMacro = macroBody("cxSetBranding");
ok(/GetDlgItem \$0 \$HWNDPARENT 1028[\s\S]*?WM_SETTEXT[\s\S]*?GetDlgItem \$0 \$HWNDPARENT 1256[\s\S]*?WM_SETTEXT/.test(brandMacro), "cxSetBranding 必须同时写 1028（可见文字）与 1256（刻线遮罩）");
ok(/!ifndef BUILD_UNINSTALLER[\s\S]*?MUI_CUSTOMFUNCTION_GUIINIT cxGuiInit[\s\S]*?Function cxGuiInit[\s\S]*?cxSetBranding[\s\S]*?!else[\s\S]*?MUI_CUSTOMFUNCTION_UNGUIINIT un\.cxGuiInit[\s\S]*?Function un\.cxGuiInit[\s\S]*?cxSetBranding[\s\S]*?!endif/.test(text), "品牌栏 GUIINIT 函数必须按 BUILD_UNINSTALLER 分支各定义一份（cxGuiInit / un.cxGuiInit）");
ok(en.has("cxBranding"), "缺 cxBranding LangString（品牌栏文案）");

// 元数据纪律：安装包 exe 的 FileDescription/CompanyName/版权串与卸载列表「发布者」都来自
// 这几个字段，不得再出现内部代号（appId 例外——改它会让升级链认成另一款软件，不检查）。
ok(!/telegram-mtproto-ai|桌面壳|FastAPI/.test(String(pkg.author) + String(pkg.description)), "package.json author/description 出现内部代号（会印在安装包属性与「应用和功能」发布者栏）");
ok(pkg.build && typeof pkg.build.copyright === "string" && /无界科技|BOUNDLESS/.test(pkg.build.copyright), "build.copyright 必须显式写无界科技口径（默认串会拼成 Copyright © <year> <author 旧值>）");

// ---- 自指：本测试挂进 test 与 predist 三链（防未来掉链） ----------------------
for (const key of ["test", "predist", "predist:win", "predist:win:clean"]) {
  const s = (pkg.scripts && pkg.scripts[key]) || "";
  ok(s.includes("uninstall-nsh-invariants"), "package.json scripts." + key + " 未挂 uninstall-nsh-invariants.test.js");
}

console.log("uninstall-nsh-invariants: " + passed + " assertions passed");
