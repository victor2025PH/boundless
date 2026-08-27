/**
 * 登录浏览器「窗口模式」决策（纯函数 + 零依赖 + 可单测）。
 *
 * B65（2026-08-24 老板拍板）：登录期窗口方针反转——**登录前的各个页面不许藏，主动弹出**
 * （账密/2FA/checkpoint 都发生在真窗口里，人看得见、点得到；offscreen 曾把 checkpoint
 * 「继续」按钮藏到屏外导致登录卡死，见 server.js B64 二期注释）；**登录成功后窗口自动隐藏**
 * （server.js::maybeAutoHideAfterLogin 无头回归——授权后窗口的使命已结束，继续可见就是
 * 「三个页面来回刷新」的木偶秀，用户会去点/关它反而干扰自动化）。
 * 四种模式：
 *   headed        — 可见窗口（交互/非交互登录的默认；人直接在窗口里操作）
 *   offscreen     — headed 但挪到所有显示器之外：无可见窗口 + 保留 headed 的最强反检测
 *                   （截图 snapshot 仍可用，因为页面照常渲染；改为 env 可选项）
 *   new_headless  — Chrome 新版 headless（--headless=new，须真 Chrome 通道）：无窗口，
 *                   比经典 headless 难检测；给无显示器的服务器部署用
 *   headless      — 经典 headless（restore/自愈已授权会话用，无需人工）
 *
 * 为何登录期不用 headless：经典 headless 会改变 FB 的检测面（navigator.webdriver /
 * UA / 渲染差异），是「账密验证码都对却被弹回登录页」的高发因（见 server.js BROWSER_CHANNEL
 * 注释）。headed 反检测最强且人可兜底；无显示器服务器部署仍可 env 切
 * new_headless / headless / offscreen（MSG_INTERACTIVE_WINDOW）。
 *
 * 本模块**只做决策**（给 headless 布尔 + 额外 args + 模式名）；真正 launch 在 server.js。
 */

export const WINDOW_MODE = {
  HEADED: "headed",
  OFFSCREEN: "offscreen",
  NEW_HEADLESS: "new_headless",
  HEADLESS: "headless",
};

// 离屏坐标取所有显示器之外的极值 + 给一个正常窗口尺寸（否则某些环境会给 0 尺寸视口）。
const _OFFSCREEN_ARGS = ["--window-position=-32000,-32000", "--window-size=1280,800"];

/** 交互登录窗口模式（env MSG_INTERACTIVE_WINDOW 选；非法/空 → 默认 headed）。
 *  B65：默认从 offscreen 反转为 headed——登录前窗口必须可见（老板拍板；117 曾以
 *  User env MSG_INTERACTIVE_WINDOW=headed 做过同语义的本机 stopgap，现收编为默认）。 */
export function interactiveWindowMode(envVal) {
  const v = String(envVal || "").trim().toLowerCase();
  if (v === WINDOW_MODE.HEADED || v === WINDOW_MODE.NEW_HEADLESS
      || v === WINDOW_MODE.HEADLESS || v === WINDOW_MODE.OFFSCREEN) {
    return v;
  }
  return WINDOW_MODE.HEADED;
}

/** 模式 → 启动配置 { headless, args, mode }（args 为副本，调用方改不脏内部常量）。 */
export function launchForMode(mode) {
  switch (mode) {
    case WINDOW_MODE.HEADED:
      return { headless: false, args: [], mode: WINDOW_MODE.HEADED };
    case WINDOW_MODE.HEADLESS:
      return { headless: true, args: [], mode: WINDOW_MODE.HEADLESS };
    case WINDOW_MODE.NEW_HEADLESS:
      // 新版 headless：headless:false 走真 Chrome 渲染，--headless=new 交给 Chrome 生效
      // （须 channel:chrome；Playwright 不会因 headless:false 而剥掉这条 arg）。
      return { headless: false, args: ["--headless=new"], mode: WINDOW_MODE.NEW_HEADLESS };
    case WINDOW_MODE.OFFSCREEN:
    default:
      return { headless: false, args: _OFFSCREEN_ARGS.slice(), mode: WINDOW_MODE.OFFSCREEN };
  }
}

/**
 * (交互?, restore?, 经典 HEADLESS env, RESTORE_HEADLESS env, 交互窗口模式) → 启动配置。
 * 单一事实源：把 server.js::startLogin 里原本 `isRestore ? RESTORE_HEADLESS : HEADLESS` 的
 * 内联判断收拢到这里，并叠加交互登录的窗口模式分支。**非交互 / restore 分支逐字节保持旧
 * 行为**（有门禁钉住），交互分支才引入 offscreen/new_headless。
 *
 * @param {object} o
 * @param {boolean} o.interactive         本次是否交互登录（Form-Relay）
 * @param {boolean} o.isRestore           是否 restore/自愈已授权会话
 * @param {boolean} o.headlessEnv         经典 HEADLESS env（MSG_HEADLESS）
 * @param {boolean} o.restoreHeadlessEnv  RESTORE_HEADLESS env（MSG_RESTORE_HEADLESS）
 * @param {string}  [o.interactiveMode]   MSG_INTERACTIVE_WINDOW
 * @returns {{headless:boolean, args:string[], mode:string}}
 */
export function resolveLaunch(o) {
  const opts = o || {};
  // restore / 自愈：与交互登录正交，永远走既有 restore 语义（已授权会话，无需人工）。
  if (opts.isRestore) {
    return launchForMode(opts.restoreHeadlessEnv ? WINDOW_MODE.HEADLESS : WINDOW_MODE.HEADED);
  }
  // 非交互（旧路径）：保持现状——交互登录默认 headed，MSG_HEADLESS 可覆盖成 headless。
  if (!opts.interactive) {
    return launchForMode(opts.headlessEnv ? WINDOW_MODE.HEADLESS : WINDOW_MODE.HEADED);
  }
  // 交互登录（Form-Relay）：按窗口模式（env 可选，B65 起默认 headed＝登录期窗口可见）。
  return launchForMode(interactiveWindowMode(opts.interactiveMode));
}

/**
 * B65：登录成功后可见窗口是否自动无头回归（server.js::maybeAutoHideAfterLogin 消费）。
 *
 * 只对 **headed**（真可见）窗口成立——offscreen / new_headless / headless 本就不可见，
 * 换无头是纯折腾（多一次 FB 导航面）。且必须 RESTORE_HEADLESS 为真：restore 语义配置成
 * headed（调试档）时换头等于「关一个可见窗、开另一个可见窗」的死循环，此时不动。
 * hideEnv（MSG_HIDE_AFTER_LOGIN，默认开）是运维逃生门：置 0 回到「窗口留到人手关」旧行为。
 *
 * @param {object} o {windowMode, restoreHeadlessEnv, hideEnv}
 * @returns {boolean}
 */
export function shouldAutoHideAfterLogin(o) {
  const opts = o || {};
  return opts.windowMode === WINDOW_MODE.HEADED
    && !!opts.restoreHeadlessEnv
    && !!opts.hideEnv;
}
