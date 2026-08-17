/**
 * 登录页分类器门禁（node --test，零依赖）。
 *
 * 用真实 Facebook 登录链路的 URL 样本钉住判据。重点不是「认得出」，而是
 * **不误报**：把「正常等人输入密码」报成「密码错」会让运维去查根本不存在的故障。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyLoginPage, actionableCode, STAGE, isE2eePinText,
} from "./login_classify.js";

const sig = (o) => ({ url: "", hasCUser: false, hasXs: false, hasErrorBox: false, ...o });

test("二因子：显式 URL 三形态", () => {
  for (const url of [
    "https://www.facebook.com/two_step_verification/authentication/?next=",
    "https://www.facebook.com/authentication/?next=%2F",
    "https://www.facebook.com/checkpoint/1501092823525282/?next",
  ]) {
    assert.equal(classifyLoginPage(sig({ url })), STAGE.TWO_FACTOR, url);
  }
});

test("二因子：cookie 签名（c_user 已下发、xs 未下发）——URL 认不出也成立", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/xyz-brand-new-page/", hasCUser: true })),
    STAGE.TWO_FACTOR);
});

test("检查点：泛 checkpoint 归 checkpoint，不被误判成二因子", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/checkpoint/828281030927956/", hasCUser: true })),
    STAGE.CHECKPOINT);
});

test("检查点优先于 cookie 签名（checkpoint 页同样已下发 c_user）", () => {
  const s = sig({ url: "https://www.facebook.com/checkpoint/block/?next", hasCUser: true, hasXs: false });
  assert.equal(classifyLoginPage(s), STAGE.CHECKPOINT);
});

test("密码错：错误框在 messenger.com 根路径（URL 里没有 /login）也算数", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasErrorBox: true })),
    STAGE.PASSWORD_ERROR);
});

test("不误报：干净的登录表单是正常等待态，不是错误", () => {
  for (const url of [
    "https://www.facebook.com/login/",
    "https://www.facebook.com/login.php?skip_api_login=1",
    "https://www.facebook.com/login/device-based/regular/login/",
    "https://www.facebook.com/recover/initiate/",
  ]) {
    assert.equal(classifyLoginPage(sig({ url })), STAGE.LOGIN_FORM, url);
  }
});

test("不误报：认不出的页面归 unknown，不硬凑一个原因", () => {
  assert.equal(classifyLoginPage(sig({ url: "https://www.messenger.com/t/12345" })), STAGE.UNKNOWN);
  assert.equal(classifyLoginPage(sig({ url: "about:blank" })), STAGE.UNKNOWN);
  assert.equal(classifyLoginPage(sig({})), STAGE.UNKNOWN);
});

test("DOM 兜底：messenger.com 根路径登录页（URL 无 /login 但有账密框）→ login_form", () => {
  // 2026-08-13 canary 实测：未登录时 goto messenger.com 落在根路径，URL 无 /login 标记，
  // 只靠 URL 会误判 unknown → 中继流永远停在 wait。hasLoginForm 兜住。
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasLoginForm: true })),
    STAGE.LOGIN_FORM);
});

test("DOM 兜底：已完整登录（c_user+xs 双全）即便残留账密框也不判 login_form", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasLoginForm: true,
      hasCUser: true, hasXs: true })),
    STAGE.UNKNOWN);
});

test("DOM 兜底优先级：更具体的段（2FA/检查点/错误框/E2EE）仍压过 hasLoginForm", () => {
  // 有账密框但 URL 是检查点 → 检查点（更具体、可操作）
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/checkpoint/x", hasLoginForm: true })),
    STAGE.CHECKPOINT);
  // 有账密框但挂着错误框 → 密码错（更可操作）
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasLoginForm: true, hasErrorBox: true })),
    STAGE.PASSWORD_ERROR);
});

test("已完整登录（c_user + xs 都有）不该被判成二因子", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasCUser: true, hasXs: true })),
    STAGE.UNKNOWN);
});

test("E2EE PIN：URL 关键词或页面文案命中（半死态重登路径）", () => {
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/t/123?encrypted_backup=1",
      hasCUser: true, hasXs: true,
    })),
    STAGE.E2EE_PIN);
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/",
      hasCUser: true, hasXs: true,
      pageText: "Enter your PIN to restore encrypted chats",
    })),
    STAGE.E2EE_PIN);
});

test("E2EE PIN：纯加密线程 URL 不是 PIN 证据（P4 2026-08-15 boot 误报实锤）", () => {
  // 重启恢复落在上次打开的加密会话页：URL /e2ee/t/<数字> 含裸 "e2ee"，旧判据
  // 把正常会话页误分类成 e2ee_pin → tryAutoE2eePin 空转 + 坐席看到误导性
  // 「请填 PIN」提示码。纯线程路径 + 无 PIN 文案 = 已登录正常态，归 unknown。
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/e2ee/t/1054679137531045/",
      hasCUser: true, hasXs: true,
    })),
    STAGE.UNKNOWN);
  // 线程页上叠了真 PIN 浮层：URL 判据被豁免，但文案判据必须兜住
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/e2ee/t/1054679137531045/",
      hasCUser: true, hasXs: true,
      pageText: "输入你的 PIN 以恢复端到端加密聊天",
    })),
    STAGE.E2EE_PIN);
  // 具体标记（encryption_pin 等）叠在线程 URL 查询串上仍算证据（豁免只针对裸 e2ee）
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/e2ee/t/999?entrypoint=encryption_pin",
      hasCUser: true, hasXs: true,
    })),
    STAGE.E2EE_PIN);
  // 非线程的 e2ee 流程页（如 /e2ee/restore）不受豁免影响
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/e2ee/restore",
      hasCUser: true, hasXs: true,
    })),
    STAGE.E2EE_PIN);
});

test("只有可操作的段落出网，正常等待态返回空串", () => {
  assert.equal(actionableCode(STAGE.TWO_FACTOR), "two_factor");
  assert.equal(actionableCode(STAGE.CHECKPOINT), "checkpoint");
  assert.equal(actionableCode(STAGE.PASSWORD_ERROR), "password_error");
  assert.equal(actionableCode(STAGE.E2EE_PIN), "e2ee_pin");
  assert.equal(actionableCode(STAGE.LOGIN_FORM), "");
  assert.equal(actionableCode(STAGE.UNKNOWN), "");
  assert.equal(actionableCode(undefined), "");
});

test("isE2eePinText：PIN 文案判据（auto-PIN 只在命中时碰键盘）", () => {
  assert.equal(isE2eePinText("Enter your PIN to restore encrypted chats"), true);
  assert.equal(isE2eePinText("请输入你的 PIN 码以恢复加密聊天"), true);
  assert.equal(isE2eePinText("恢复端到端加密聊天记录"), true);
  assert.equal(isE2eePinText("Welcome back to Messenger"), false);
  assert.equal(isE2eePinText(""), false);
  assert.equal(isE2eePinText(null), false);
});

test("入参防御：脏输入不抛异常（观测绝不能弄崩登录看门狗）", () => {
  assert.doesNotThrow(() => classifyLoginPage(null));
  assert.doesNotThrow(() => classifyLoginPage({ url: 42 }));
  assert.equal(classifyLoginPage(null), STAGE.UNKNOWN);
});
