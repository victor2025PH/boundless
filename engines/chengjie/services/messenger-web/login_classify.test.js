/**
 * 登录页分类器门禁（node --test，零依赖）。
 *
 * 用真实 Facebook 登录链路的 URL 样本钉住判据。重点不是「认得出」，而是
 * **不误报**：把「正常等人输入密码」报成「密码错」会让运维去查根本不存在的故障。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyLoginPage, actionableCode, checkpointFlavor, STAGE, isE2eePinText,
  isTemporarilyBlockedText,
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

test("B64 机器人验证：可见文案「确认您是真人」在非 /checkpoint/ 路径也归 checkpoint", () => {
  // `_316` 实录：验证组件渲染在过渡白页上，URL 认不出 → 旧判据只能回 unknown，
  // 前端一句恒定的「等待登录」，用户干等一个永远不会来的 2FA 码。
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/", pageText: "确认您是真人，请完成下方验证" })),
    STAGE.CHECKPOINT);
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", pageText: "Confirm you're human to continue" })),
    STAGE.CHECKPOINT);
});

test("B64 机器人验证：压过 cookie 二因子推断（验证不过、码不会发，等码是误导）", () => {
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.facebook.com/some-transition/",
      hasCUser: true, hasXs: false,
      pageText: "确认您是真人",
    })),
    STAGE.CHECKPOINT);
});

test("B64 机器人验证：已完整登录时正文提到这句话不构成证据", () => {
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.messenger.com/",
      hasCUser: true, hasXs: true,
      pageText: "他发来一句：确认您是真人",
    })),
    STAGE.UNKNOWN);
});

test("B64 机器人验证：泛词「security check」不触发（2FA 说明文字常含它，防误报）", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/login/", pageText: "This is a security check page" })),
    STAGE.LOGIN_FORM);
});

test("B64 检查点变体：checkpoint_interstitial 不得被 /login 前缀截胡（2026-08-23 23:55 实录）", () => {
  // relay-submit 提交 0.7s 后 FB 押送到这个 URL，旧判据判成 login_form →
  // 表单接口继续说「请输入账密」→ 用户视角「点了登录没反应」。
  const real = "https://www.messenger.com/login/checkpoint_interstitial/?next=https%3A%2F%2F"
    + "www.facebook.com%2Fcheckpoint%2Fstart%2F%3Fip%3D27.126.158.220&is_msplit_account=0";
  assert.equal(classifyLoginPage(sig({ url: real })), STAGE.CHECKPOINT);
});

test("B64 检查点变体：checkpoint 只藏在编码 next= 参数里也认得出", () => {
  assert.equal(
    classifyLoginPage(sig({
      url: "https://www.facebook.com/some_gate/?next=https%3A%2F%2Fwww.facebook.com%2Fcheckpoint%2F828%2F",
    })),
    STAGE.CHECKPOINT);
});

test("B64 检查点变体：坏编码 URL 不抛异常（观测绝不弄崩看门狗）", () => {
  assert.doesNotThrow(() => classifyLoginPage(sig({ url: "https://x.com/a%E0%A4%A" })));
});

test("只有可操作的段落出网，正常等待态返回空串", () => {
  assert.equal(actionableCode(STAGE.TWO_FACTOR), "two_factor");
  assert.equal(actionableCode(STAGE.CHECKPOINT), "checkpoint");
  assert.equal(actionableCode(STAGE.ACCOUNT_LOCKED), "account_locked");
  assert.equal(actionableCode(STAGE.PASSWORD_ERROR), "password_error");
  assert.equal(actionableCode(STAGE.E2EE_PIN), "e2ee_pin");
  assert.equal(actionableCode(STAGE.LOGIN_FORM), "");
  assert.equal(actionableCode(STAGE.UNKNOWN), "");
  assert.equal(actionableCode(undefined), "");
});

test("P1 账号锁定：中英句式归 account_locked，且压过泛检查点 URL（出路不同）", () => {
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/checkpoint/block/",
      pageText: "You're temporarily blocked from doing this" })),
    STAGE.ACCOUNT_LOCKED);
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/login/",
      pageText: "尝试次数过多，请稍后再试" })),
    STAGE.ACCOUNT_LOCKED);
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.facebook.com/", pageText: "你已被暂时封锁" })),
    STAGE.ACCOUNT_LOCKED);
  // 已完整登录时正文提到锁定词不构成证据
  assert.equal(
    classifyLoginPage(sig({ url: "https://www.messenger.com/", hasCUser: true, hasXs: true,
      pageText: "too many attempts" })),
    STAGE.UNKNOWN);
});

test("isE2eePinText：PIN 文案判据（auto-PIN 只在命中时碰键盘）", () => {
  assert.equal(isE2eePinText("Enter your PIN to restore encrypted chats"), true);
  assert.equal(isE2eePinText("请输入你的 PIN 码以恢复加密聊天"), true);
  assert.equal(isE2eePinText("恢复端到端加密聊天记录"), true);
  assert.equal(isE2eePinText("Welcome back to Messenger"), false);
  assert.equal(isE2eePinText(""), false);
  assert.equal(isE2eePinText(null), false);
});

test("isTemporarilyBlockedText：B99 临时封锁判据（宁漏勿误——误判=好号被冻 2h）", () => {
  // 命中：0825 _485 实录形态 + 常见英文变体
  assert.equal(isTemporarilyBlockedText("You're Temporarily Blocked"), true);
  assert.equal(isTemporarilyBlockedText("You are temporarily blocked from using this feature"), true);
  assert.equal(isTemporarilyBlockedText("You're temporarily restricted"), true);
  assert.equal(isTemporarilyBlockedText("你已被暂时阻止使用该功能"), true);
  assert.equal(isTemporarilyBlockedText("您被暂时封锁"), true);
  assert.equal(isTemporarilyBlockedText("我们暂时封锁了你的帐户功能"), true);
  assert.equal(isTemporarilyBlockedText("你已被暫時封鎖"), true);
  // 不命中：泛泛的稍后再试/普通聊天内容/永久封禁措辞（那是 ACCOUNT_LOCKED 的领地）
  assert.equal(isTemporarilyBlockedText("Please try again later"), false);
  assert.equal(isTemporarilyBlockedText("我暂时不方便，晚点聊"), false);
  assert.equal(isTemporarilyBlockedText("Your account has been disabled"), false);
  assert.equal(isTemporarilyBlockedText(""), false);
  assert.equal(isTemporarilyBlockedText(null), false);
});

test("入参防御：脏输入不抛异常（观测绝不能弄崩登录看门狗）", () => {
  assert.doesNotThrow(() => classifyLoginPage(null));
  assert.doesNotThrow(() => classifyLoginPage({ url: 42 }));
  assert.equal(classifyLoginPage(null), STAGE.UNKNOWN);
});

// ── B64 三期：checkpoint 子味（等手机确认 vs 机器人验证/泛检查点）──

test("B64三期 checkpointFlavor：确认类句式归 device_confirm（2026-08-24 实录页）", () => {
  // 实录：手机确认后登录页停在「请验证你的 Facebook 帐户 → [继续]」
  assert.equal(checkpointFlavor("请验证你的 Facebook 帐户 只需要几分钟"), "device_confirm");
  assert.equal(checkpointFlavor("請驗證你的 Facebook 帳戶"), "device_confirm");
  assert.equal(checkpointFlavor("我们已向你的手机发送了一条通知，请在手机上确认"), "device_confirm");
  assert.equal(checkpointFlavor("Check your notifications on another device"), "device_confirm");
  assert.equal(checkpointFlavor("We sent a notification to your phone. Approve this login."), "device_confirm");
  assert.equal(checkpointFlavor("Waiting for approval from your device"), "device_confirm");
});

test("B64三期 checkpointFlavor：机器人验证/泛检查点/空文本不归 device_confirm", () => {
  // 机器人验证：用户必须盯着画面过验证，引去手机上空找通知=指错路（显式排除）
  assert.equal(checkpointFlavor("请完成安全验证 确认您是真人"), "");
  assert.equal(checkpointFlavor("Confirm you're human to continue"), "");
  // 机器人验证与确认句式同现：宁保守走截图为主
  assert.equal(checkpointFlavor("确认您是真人 请验证你的 Facebook 帐户"), "");
  // 泛词不构成证据（verify/confirm 单独出现在太多页面上）
  assert.equal(checkpointFlavor("Please verify to continue"), "");
  assert.equal(checkpointFlavor("Security check required"), "");
  assert.equal(checkpointFlavor(""), "");
  assert.equal(checkpointFlavor(null), "");
});
