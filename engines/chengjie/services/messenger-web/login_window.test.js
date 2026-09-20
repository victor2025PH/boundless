/**
 * 登录窗口模式决策门禁（node --test，零依赖）。
 *
 * 钉住三类不变量：① 交互登录默认 **headed 可见窗口**（B65 老板拍板：登录前的各个页面
 * 不许藏、主动弹出；旧默认 offscreen 降级为 env 可选项），可 env 切模式；
 * ② **非交互 / restore 分支逐字节保持旧行为**（isRestore?RESTORE_HEADLESS:HEADLESS）——
 * 这条是「新功能不许悄悄改既有登录路径」的回归网；
 * ③ 登录成功后自动隐藏判定 shouldAutoHideAfterLogin 只对「headed 可见 + restore 无头 +
 * 开关开」三条件齐备时为真（防「关一个可见窗、开另一个可见窗」死循环）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  WINDOW_MODE, interactiveWindowMode, launchForMode, resolveLaunch,
  shouldAutoHideAfterLogin,
} from "./login_window.js";

test("interactiveWindowMode：默认 headed（B65）；合法值透传；非法/空回默认；大小写不敏感", () => {
  assert.equal(interactiveWindowMode(""), WINDOW_MODE.HEADED);
  assert.equal(interactiveWindowMode(null), WINDOW_MODE.HEADED);
  assert.equal(interactiveWindowMode("garbage"), WINDOW_MODE.HEADED);
  assert.equal(interactiveWindowMode("HEADED"), WINDOW_MODE.HEADED);
  assert.equal(interactiveWindowMode(" new_headless "), WINDOW_MODE.NEW_HEADLESS);
  assert.equal(interactiveWindowMode("headless"), WINDOW_MODE.HEADLESS);
  assert.equal(interactiveWindowMode("offscreen"), WINDOW_MODE.OFFSCREEN);
});

test("launchForMode：headed 可见、offscreen 离屏、new_headless 带 --headless=new、headless 无头", () => {
  assert.deepEqual(launchForMode(WINDOW_MODE.HEADED), { headless: false, args: [], mode: "headed" });
  assert.deepEqual(launchForMode(WINDOW_MODE.HEADLESS), { headless: true, args: [], mode: "headless" });

  const nh = launchForMode(WINDOW_MODE.NEW_HEADLESS);
  assert.equal(nh.headless, false);
  assert.ok(nh.args.includes("--headless=new"));

  const off = launchForMode(WINDOW_MODE.OFFSCREEN);
  assert.equal(off.headless, false);
  assert.ok(off.args.some((a) => a.startsWith("--window-position=")));
});

test("launchForMode：未知模式回落 offscreen（无窗口默认，不误开可见窗）", () => {
  assert.equal(launchForMode("nope").mode, WINDOW_MODE.OFFSCREEN);
});

test("launchForMode：args 为副本，改不脏内部常量", () => {
  const a = launchForMode(WINDOW_MODE.OFFSCREEN);
  a.args.push("--injected");
  assert.ok(!launchForMode(WINDOW_MODE.OFFSCREEN).args.includes("--injected"));
});

// ── 回归网：非交互 / restore 分支必须逐字节等于旧行为 ─────────────────────────
test("非交互 + HEADLESS=false → headed（现行交互登录默认，零回归）", () => {
  const r = resolveLaunch({ interactive: false, isRestore: false, headlessEnv: false, restoreHeadlessEnv: true });
  assert.equal(r.headless, false);
  assert.deepEqual(r.args, []);
});

test("非交互 + HEADLESS=true → headless（MSG_HEADLESS 覆盖，零回归）", () => {
  const r = resolveLaunch({ interactive: false, isRestore: false, headlessEnv: true, restoreHeadlessEnv: true });
  assert.equal(r.headless, true);
});

test("restore + RESTORE_HEADLESS=true → headless（自愈默认无头，零回归）", () => {
  const r = resolveLaunch({ isRestore: true, restoreHeadlessEnv: true, headlessEnv: false });
  assert.equal(r.headless, true);
});

test("restore + RESTORE_HEADLESS=false → headed（旧「restore 也 headed」逃生门，零回归）", () => {
  const r = resolveLaunch({ isRestore: true, restoreHeadlessEnv: false, headlessEnv: true });
  assert.equal(r.headless, false);
});

test("restore 优先于 interactive（已授权会话自愈永不走交互窗口模式）", () => {
  const r = resolveLaunch({
    isRestore: true, interactive: true, restoreHeadlessEnv: true,
    interactiveMode: "offscreen",
  });
  assert.equal(r.headless, true);
  assert.equal(r.mode, WINDOW_MODE.HEADLESS);
});

// ── 交互登录分支 ─────────────────────────────────────────────────────────────
test("交互 + 未指定模式 → headed 可见窗口（B65：登录前不许藏）", () => {
  const r = resolveLaunch({ interactive: true, isRestore: false, headlessEnv: false });
  assert.equal(r.headless, false);
  assert.equal(r.mode, WINDOW_MODE.HEADED);
  assert.deepEqual(r.args, []);
});

test("交互 + MSG_INTERACTIVE_WINDOW=offscreen → 离屏仍可选（env 逃生门）", () => {
  const r = resolveLaunch({ interactive: true, interactiveMode: "offscreen" });
  assert.equal(r.headless, false);
  assert.equal(r.mode, WINDOW_MODE.OFFSCREEN);
  assert.ok(r.args.some((a) => a.startsWith("--window-position=")));
});

test("交互 + MSG_INTERACTIVE_WINDOW=new_headless → 无窗口无头新版（服务器部署）", () => {
  const r = resolveLaunch({ interactive: true, interactiveMode: "new_headless" });
  assert.equal(r.mode, WINDOW_MODE.NEW_HEADLESS);
  assert.ok(r.args.includes("--headless=new"));
});

test("交互 + MSG_INTERACTIVE_WINDOW=headed → 显式 headed（与默认同义）", () => {
  const r = resolveLaunch({ interactive: true, interactiveMode: "headed" });
  assert.equal(r.mode, WINDOW_MODE.HEADED);
  assert.equal(r.headless, false);
  assert.deepEqual(r.args, []);
});

test("交互 + MSG_INTERACTIVE_WINDOW=headless → 经典无头（对照组）", () => {
  const r = resolveLaunch({ interactive: true, interactiveMode: "headless" });
  assert.equal(r.headless, true);
});

// ── B65：登录成功后自动隐藏判定 ──────────────────────────────────────────────
test("shouldAutoHideAfterLogin：headed + restore无头 + 开关开 → 真", () => {
  assert.equal(shouldAutoHideAfterLogin(
    { windowMode: "headed", restoreHeadlessEnv: true, hideEnv: true }), true);
});

test("shouldAutoHideAfterLogin：不可见模式（offscreen/new_headless/headless）恒假", () => {
  for (const m of ["offscreen", "new_headless", "headless"]) {
    assert.equal(shouldAutoHideAfterLogin(
      { windowMode: m, restoreHeadlessEnv: true, hideEnv: true }), false, m);
  }
});

test("shouldAutoHideAfterLogin：RESTORE_HEADLESS=0（restore 也 headed）→ 假（防换头死循环）", () => {
  assert.equal(shouldAutoHideAfterLogin(
    { windowMode: "headed", restoreHeadlessEnv: false, hideEnv: true }), false);
});

test("shouldAutoHideAfterLogin：MSG_HIDE_AFTER_LOGIN=0 逃生门 → 假", () => {
  assert.equal(shouldAutoHideAfterLogin(
    { windowMode: "headed", restoreHeadlessEnv: true, hideEnv: false }), false);
});

test("shouldAutoHideAfterLogin：空入参不抛、判假", () => {
  assert.equal(shouldAutoHideAfterLogin(undefined), false);
  assert.equal(shouldAutoHideAfterLogin({}), false);
});
