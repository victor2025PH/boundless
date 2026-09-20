/**
 * 表单中继登录（Form-Relay）——把登录页分类（login_classify）翻成「应用此刻该渲染哪一张
 * 原生分步表单」的契约（纯函数 + 零依赖 + 可单测）。
 *
 * 背景：Messenger 托管登录此前只有两条不够好的路——
 *   ① headed 弹一个独立浏览器窗口让人手输账密（用户要满屏找窗口，远程坐席根本够不着）；
 *   ② 只读截图「实时预览」（看得见、点不动，做成浏览器窗体样式反而暗示可交互）。
 * 表单中继是第三条：边车 headless 起浏览器（不再弹窗），classifyLoginPage 判出「FB 此刻
 * 在要什么」，应用据此渲染品牌化的原生输入（账密 / 2FA 码 / E2EE PIN），值回传边车填进
 * 页面。桌面壳与远程浏览器坐席体验一致，且没有截图直播的 IME / 坐标 / 帧延迟三座大山。
 *
 * 分工：本模块只管**语义契约**（现在是哪一步、要渲染哪些字段、要不要回落截图）。真正的
 * DOM 选择器与 page.fill / keyboard 注入是 server.js 的活（非纯，不进本模块，且要拿真实
 * 登录页验证选择器）。词汇与 login_classify.STAGE 同源，避免再养一张会漂移的映射表。
 *
 * 反检测注记（务必随本模块一起读）：表单中继需要边车 headless + 由边车把值填进登录页，
 * 这与现行「headed + 真人手输」相比改变了 Facebook 的自动化判定面。故上层一律 feature-flag
 * 默认关、保留 headed 为默认与回落，放量前需灰度验证账号不被风控。本模块是纯语义层，
 * 不决定「要不要开」——只定义「开了之后各步长什么样」。
 */

import { STAGE } from "./login_classify.js";

/**
 * 中继步骤（前端据此渲染原生表单；与截图预览互斥）。
 *   credentials — 账密页：渲 email + password
 *   twofactor   — 第二因子：渲 6 位验证码
 *   e2ee_pin    — 端到端加密设备 PIN / 密钥恢复
 *   checkpoint  — 不可预测的安全检查点 → 前端回落截图预览 / 转交运维（本方案不硬闯）
 *   done        — 已授权，收摊
 *   wait        — 加载中 / 认不出的页面 → 前端显示「准备中」，下一轮轮询再判
 */
export const RELAY_STEP = {
  CREDENTIALS: "credentials",
  TWO_FACTOR: "twofactor",
  E2EE_PIN: "e2ee_pin",
  CHECKPOINT: "checkpoint",
  DONE: "done",
  WAIT: "wait",
};

// 每个中继步骤要渲染的原生字段（前端照此建输入框；空数组＝无输入，纯等待/回落）。
const STEP_FIELDS = {
  [RELAY_STEP.CREDENTIALS]: ["email", "password"],
  [RELAY_STEP.TWO_FACTOR]: ["code"],
  [RELAY_STEP.E2EE_PIN]: ["pin"],
  [RELAY_STEP.CHECKPOINT]: [],
  [RELAY_STEP.DONE]: [],
  [RELAY_STEP.WAIT]: [],
};

/** 某步骤要渲染 / 接受的字段名列表（返回副本，调用方改不脏内部表）。 */
export function relayFieldsFor(step) {
  return (STEP_FIELDS[step] || []).slice();
}

function _mk(step, extra) {
  return {
    step,
    fields: relayFieldsFor(step),
    // password_error：账密页挂着错误框——回 credentials 步但标 error，前端提示「重填」。
    error: !!(extra && extra.error),
    // checkpoint：不可预测，前端回落截图预览 + 转交运维，别拿原生表单硬闯。
    escalate: !!(extra && extra.escalate),
    // 与 login_classify.actionableCode 同一套原因码，供前端出本地化文案 / 埋点。
    code: (extra && extra.code) || "",
  };
}

/**
 * (分类段, 是否已授权) → 中继步骤描述。单一事实源：login_classify 判「在哪一段」，
 * 本函数只做「段 → 该渲染什么」的确定性映射，不引入新判据。
 *
 * @param {string} stage  login_classify.classifyLoginPage 的输出（STAGE.*）
 * @param {object} [opts]
 * @param {boolean} [opts.authorized] 会话是否已授权（c_user + xs 齐全 / 已 promote）
 * @returns {{step:string, fields:string[], error:boolean, escalate:boolean, code:string}}
 */
export function relayStepFromStage(stage, opts) {
  const authorized = !!(opts && opts.authorized);
  // 已授权优先于一切：即便某轮 URL 恰好还停在过渡页，也不该再要用户输入。
  if (authorized) return _mk(RELAY_STEP.DONE);
  switch (stage) {
    case STAGE.LOGIN_FORM:
      return _mk(RELAY_STEP.CREDENTIALS);
    case STAGE.PASSWORD_ERROR:
      return _mk(RELAY_STEP.CREDENTIALS, { error: true, code: "password_error" });
    case STAGE.TWO_FACTOR:
      return _mk(RELAY_STEP.TWO_FACTOR, { code: "two_factor" });
    case STAGE.E2EE_PIN:
      return _mk(RELAY_STEP.E2EE_PIN, { code: "e2ee_pin" });
    case STAGE.CHECKPOINT:
      return _mk(RELAY_STEP.CHECKPOINT, { escalate: true, code: "checkpoint" });
    case STAGE.ACCOUNT_LOCKED:
      // 锁定＝检查点家族（同走截图回落视图），但 code 单列——前端据此给「等冷却/
      // 去申诉」而非「当场过验证」的出路（两者混谈=让用户拿错药方）。
      return _mk(RELAY_STEP.CHECKPOINT, { escalate: true, code: "account_locked" });
    default:
      // UNKNOWN / 加载中 / 认不出：不硬凑步骤，让前端等下一轮（宁可多等一轮也别错渲表单）。
      return _mk(RELAY_STEP.WAIT);
  }
}

/**
 * 校验 / 清洗前端提交的字段值：只保留该步骤的合法字段、非空。绝不抛异常（脏输入不能
 * 弄崩登录链）。返回：
 *   { accepts, ok, values, missing }
 *   - accepts=false ⇒ 该步骤不接受提交（checkpoint / done / wait 无输入字段），前端不该发；
 *   - accepts=true 且 ok=false ⇒ 有字段漏填（missing 列出），前端提示补齐，不下发边车；
 *   - ok=true ⇒ values 是干净的 {字段: 值}，可交给边车填页。
 *
 * 清洗口径：email / code / pin 去首尾空白（用户复制粘贴常带空格）；password **不 trim**
 * ——密码前后空格虽罕见但确实合法，吞掉会造成「明明对却登不上」的诡异故障。
 */
export function sanitizeSubmit(step, payload) {
  const fields = relayFieldsFor(step);
  if (fields.length === 0) {
    return { accepts: false, ok: false, values: {}, missing: [] };
  }
  const src = (payload && typeof payload === "object") ? payload : {};
  const values = {};
  const missing = [];
  for (const f of fields) {
    const raw = src[f];
    const s = (raw == null) ? "" : String(raw);
    const cleaned = (f === "password") ? s : s.trim();
    if (!cleaned) {
      missing.push(f);
      continue;
    }
    values[f] = cleaned;
  }
  return { accepts: true, ok: missing.length === 0, values, missing };
}

/**
 * 各中继步骤的「填页计划」：字段候选选择器（多重回落，仿 server.js::tryAutoE2eePin 的
 * 多属性 :visible 尝试）+ 提交动作候选。选择器用 messenger.com / facebook.com 登录页多年
 * 稳定的 name/id/autocomplete —— tryAutoE2eePin 就是靠 `input[name="pass"]/`input[name="email"]`
 * 判「是不是登录页」，可见这几个 name 是仓里已在依赖的稳定锚。**但仍须 canary 真登录页验证**
 * 后才可放量（见 messenger_web_login.interactive_login 开关说明）；执行侧（server.js）任一字段
 * 选择器全落空 → 结构化 field_not_found 回落截图 / headed，绝不盲填错框。
 *
 * e2ee_pin 刻意**不在此表**：它复用既有 tryAutoE2eePin（自带 gate/retry/cooldown/校验 +
 * 更全的 PIN 输入选择器），server.js 走委托而非通用填页。故 fillPlanFor('e2ee_pin') 返回
 * null，是「委托处理」而非「不支持」——门禁据此钉住这条分工。
 */
export const FILL_PLANS = {
  [RELAY_STEP.CREDENTIALS]: {
    fields: [
      { name: "email", selectors: [
        'input[name="email"]', '#email', 'input[type="email"]',
        'input[autocomplete="username"]',
      ] },
      { name: "password", selectors: [
        'input[name="pass"]', '#pass',
        'input[type="password"][autocomplete="current-password"]',
        'input[type="password"]',
      ] },
    ],
    submit: ['button[name="login"]', '#loginbutton', 'button[type="submit"]'],
  },
  [RELAY_STEP.TWO_FACTOR]: {
    fields: [
      { name: "code", selectors: [
        'input[name="approvals_code"]', '#approvals_code',
        'input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
      ] },
    ],
    submit: ['button[type="submit"]', 'button[name="submit[Continue]"]', '#checkpointSubmitButton'],
  },
};

/** 某步骤的填页计划；无（含 e2ee_pin 委托、checkpoint/done/wait 无输入）返回 null。 */
export function fillPlanFor(step) {
  return FILL_PLANS[step] || null;
}
