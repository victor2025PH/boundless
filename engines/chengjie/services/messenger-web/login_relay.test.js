/**
 * 表单中继契约门禁（node --test，零依赖）。
 *
 * 钉住两件事：① 分类段 → 中继步骤的映射不漂移（尤其 password_error 仍回账密步而非
 * 另起一步、checkpoint 必须标 escalate 走回落）；② sanitizeSubmit 对脏输入不抛、
 * 对无输入步骤拒收、password 不被 trim（吞空格＝「对却登不上」的诡异故障）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { STAGE } from "./login_classify.js";
import {
  RELAY_STEP, relayFieldsFor, relayStepFromStage, sanitizeSubmit,
  FILL_PLANS, fillPlanFor,
} from "./login_relay.js";

test("已授权优先于一切分类段 → done", () => {
  for (const stage of [
    STAGE.LOGIN_FORM, STAGE.TWO_FACTOR, STAGE.CHECKPOINT,
    STAGE.E2EE_PIN, STAGE.PASSWORD_ERROR, STAGE.UNKNOWN,
  ]) {
    const r = relayStepFromStage(stage, { authorized: true });
    assert.equal(r.step, RELAY_STEP.DONE, stage);
    assert.deepEqual(r.fields, []);
  }
});

test("登录表单 → credentials（渲 email + password，无 error）", () => {
  const r = relayStepFromStage(STAGE.LOGIN_FORM);
  assert.equal(r.step, RELAY_STEP.CREDENTIALS);
  assert.deepEqual(r.fields, ["email", "password"]);
  assert.equal(r.error, false);
  assert.equal(r.escalate, false);
});

test("密码错 → 仍回 credentials 步但标 error + 原因码（不另起一步，就地重填）", () => {
  const r = relayStepFromStage(STAGE.PASSWORD_ERROR);
  assert.equal(r.step, RELAY_STEP.CREDENTIALS);
  assert.deepEqual(r.fields, ["email", "password"]);
  assert.equal(r.error, true);
  assert.equal(r.code, "password_error");
});

test("二因子 → twofactor（渲 code），原因码同源", () => {
  const r = relayStepFromStage(STAGE.TWO_FACTOR);
  assert.equal(r.step, RELAY_STEP.TWO_FACTOR);
  assert.deepEqual(r.fields, ["code"]);
  assert.equal(r.code, "two_factor");
});

test("E2EE PIN → e2ee_pin（渲 pin）", () => {
  const r = relayStepFromStage(STAGE.E2EE_PIN);
  assert.equal(r.step, RELAY_STEP.E2EE_PIN);
  assert.deepEqual(r.fields, ["pin"]);
  assert.equal(r.code, "e2ee_pin");
});

test("检查点 → checkpoint 且必须 escalate（回落截图/转交运维，不拿原生表单硬闯）", () => {
  const r = relayStepFromStage(STAGE.CHECKPOINT);
  assert.equal(r.step, RELAY_STEP.CHECKPOINT);
  assert.deepEqual(r.fields, []);
  assert.equal(r.escalate, true);
  assert.equal(r.code, "checkpoint");
});

test("认不出 / 加载中 → wait（不硬凑步骤，等下一轮）", () => {
  assert.equal(relayStepFromStage(STAGE.UNKNOWN).step, RELAY_STEP.WAIT);
  assert.equal(relayStepFromStage(undefined).step, RELAY_STEP.WAIT);
  assert.equal(relayStepFromStage("brand-new-stage").step, RELAY_STEP.WAIT);
});

test("relayFieldsFor：返回副本，改不脏内部表", () => {
  const a = relayFieldsFor(RELAY_STEP.CREDENTIALS);
  a.push("injected");
  assert.deepEqual(relayFieldsFor(RELAY_STEP.CREDENTIALS), ["email", "password"]);
  assert.deepEqual(relayFieldsFor(RELAY_STEP.CHECKPOINT), []);
  assert.deepEqual(relayFieldsFor("nope"), []);
});

test("sanitizeSubmit：credentials 正常提交 → ok + 干净值", () => {
  const r = sanitizeSubmit(RELAY_STEP.CREDENTIALS, { email: "  a@b.com ", password: "pw", extra: "x" });
  assert.equal(r.accepts, true);
  assert.equal(r.ok, true);
  assert.deepEqual(r.values, { email: "a@b.com", password: "pw" });
  assert.deepEqual(r.missing, []);
});

test("sanitizeSubmit：漏填 → ok=false + missing 列名，不下发", () => {
  const r = sanitizeSubmit(RELAY_STEP.CREDENTIALS, { email: "a@b.com" });
  assert.equal(r.accepts, true);
  assert.equal(r.ok, false);
  assert.deepEqual(r.missing, ["password"]);
});

test("sanitizeSubmit：password 不 trim（前后空格是合法密码，吞掉=对却登不上）", () => {
  const r = sanitizeSubmit(RELAY_STEP.CREDENTIALS, { email: "a@b.com", password: "  spaced  " });
  assert.equal(r.ok, true);
  assert.equal(r.values.password, "  spaced  ");
});

test("sanitizeSubmit：code / pin 去首尾空白（复制粘贴常带空格）", () => {
  assert.equal(sanitizeSubmit(RELAY_STEP.TWO_FACTOR, { code: " 123456 " }).values.code, "123456");
  assert.equal(sanitizeSubmit(RELAY_STEP.E2EE_PIN, { pin: "\t0000\n" }).values.pin, "0000");
});

test("sanitizeSubmit：无输入字段的步骤不接受提交（checkpoint/done/wait）", () => {
  for (const step of [RELAY_STEP.CHECKPOINT, RELAY_STEP.DONE, RELAY_STEP.WAIT]) {
    const r = sanitizeSubmit(step, { anything: "x" });
    assert.equal(r.accepts, false, step);
    assert.equal(r.ok, false);
  }
});

test("sanitizeSubmit：脏输入不抛异常（观测/提交绝不能弄崩登录链）", () => {
  assert.doesNotThrow(() => sanitizeSubmit(RELAY_STEP.CREDENTIALS, null));
  assert.doesNotThrow(() => sanitizeSubmit(RELAY_STEP.CREDENTIALS, 42));
  assert.doesNotThrow(() => sanitizeSubmit("nope", { a: 1 }));
  const r = sanitizeSubmit(RELAY_STEP.CREDENTIALS, null);
  assert.deepEqual(r.missing, ["email", "password"]);
});

// ── 填页计划契约（Phase 1b）──────────────────────────────────────────────
test("填页计划字段必须与 sanitize 字段逐一对齐（防两侧漂移导致中继断裂）", () => {
  for (const step of [RELAY_STEP.CREDENTIALS, RELAY_STEP.TWO_FACTOR]) {
    const plan = fillPlanFor(step);
    assert.ok(plan, `${step} 应有填页计划`);
    const planFields = plan.fields.map((f) => f.name);
    assert.deepEqual(planFields, relayFieldsFor(step),
      `${step} 填页计划字段与 sanitizeSubmit 字段不一致`);
  }
});

test("填页计划：每个字段都有非空候选选择器、每步都有非空提交候选", () => {
  for (const step of [RELAY_STEP.CREDENTIALS, RELAY_STEP.TWO_FACTOR]) {
    const plan = fillPlanFor(step);
    for (const f of plan.fields) {
      assert.ok(Array.isArray(f.selectors) && f.selectors.length > 0,
        `${step}.${f.name} 选择器不能为空`);
      assert.ok(f.selectors.every((s) => typeof s === "string" && s.length > 0));
    }
    assert.ok(Array.isArray(plan.submit) && plan.submit.length > 0,
      `${step} 提交候选不能为空`);
  }
});

test("e2ee_pin 无通用填页计划（委托 tryAutoE2eePin），但仍是合法中继步", () => {
  assert.equal(fillPlanFor(RELAY_STEP.E2EE_PIN), null);
  assert.deepEqual(relayFieldsFor(RELAY_STEP.E2EE_PIN), ["pin"]);  // 委托侧仍要 pin
});

test("无输入 / 未知步骤无填页计划", () => {
  for (const step of [RELAY_STEP.CHECKPOINT, RELAY_STEP.DONE, RELAY_STEP.WAIT, "nope"]) {
    assert.equal(fillPlanFor(step), null, step);
  }
});

test("FILL_PLANS 只覆盖走通用填页的两步（credentials/twofactor）", () => {
  assert.deepEqual(Object.keys(FILL_PLANS).sort(),
    [RELAY_STEP.CREDENTIALS, RELAY_STEP.TWO_FACTOR].sort());
});
