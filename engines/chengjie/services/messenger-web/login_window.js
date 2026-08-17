/**
 * 登录浏览器「窗口模式」决策（纯函数 + 零依赖 + 可单测）。
 *
 * 交互登录（Form-Relay）的目标：**不弹可见窗口** + **尽量不改 Facebook 的自动化判定面**。
 * 四种模式：
 *   headed        — 可见窗口（非交互登录的现行默认；人直接在窗口里操作）
 *   offscreen     — headed 但挪到所有显示器之外：无可见窗口 + 保留 headed 的最强反检测
 *                   （交互登录默认；截图 snapshot 仍可用，因为页面照常渲染）
 *   new_headless  — Chrome 新版 headless（--headless=new，须真 Chrome 通道）：无窗口，
 *                   比经典 headless 难检测；给无显示器的服务器部署用
 *   headless      — 经典 headless（restore/自愈已授权会话用，无需人工）
 *
 * 为何默认 offscreen 而非 headless：经典 headless 会改变 FB 的检测面（navigator.webdriver /
 * UA / 渲染差异），是「账密验证码都对却被弹回登录页」的高发因（见 server.js BROWSER_CHANNEL
 * 注释）；offscreen 是**真** headed Chrome，只是位置在屏外，反检测与人工 headed 等价，又满足
 * 「窗口不弹到用户面前」。桌面壳（有显示器）用 offscreen；无显示器服务器切 new_headless /
 * headless（env MSG_INTERACTIVE_WINDOW）。canary 期无需改码即可 A/B 各模式的账号存活率。
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

/** 交互登录窗口模式（env MSG_INTERACTIVE_WINDOW 选；非法/空 → 默认 offscreen）。 */
export function interactiveWindowMode(envVal) {
  const v = String(envVal || "").trim().toLowerCase();
  if (v === WINDOW_MODE.HEADED || v === WINDOW_MODE.NEW_HEADLESS
      || v === WINDOW_MODE.HEADLESS || v === WINDOW_MODE.OFFSCREEN) {
    return v;
  }
  return WINDOW_MODE.OFFSCREEN;
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
  // 交互登录（Form-Relay）：按窗口模式（env 可选，默认 offscreen＝无窗口 + 最强反检测）。
  return launchForMode(interactiveWindowMode(opts.interactiveMode));
}
