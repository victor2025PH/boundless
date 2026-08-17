/**
 * 登录页状态分类（纯函数 + 零依赖 + 可单测）。
 *
 * 为什么单独成模块：托管登录是「人在服务器窗口里手动完成」，Node 只能旁观。旁观信号
 * （URL / cookie / 错误框）散在 1500ms 的看门狗循环里就永远测不到，且判据一旦写错
 * 会把「需要 2FA」误报成「密码错」——那比不报更糟。抽出来后判据可以用真实 URL 样本钉住。
 *
 * 输出词汇**刻意与 Python 侧 login_funnel_stats._REASON_CODES 同名**：
 * 既当「此刻页面在要什么」的实时提示码（hint_code），也当「窗口期结束仍没登上」的
 * 失败原因码（reason_code），两处同源省掉一张映射表（映射表是漂移的温床）。
 *
 *   two_factor     — 密码过了、卡在第二因子（验证器 / 短信码）
 *   checkpoint     — Facebook 安全检查点（风控、异常登录、账号受限）
 *   password_error — 登录表单上挂着错误框（多为密码/账号错）
 *   e2ee_pin       — 端到端加密设备 PIN / 密钥恢复页（cookie 快照恢复后常见；
 *                    须在服务器窗口完成，否则半死态读不到正文）
 *
 * 另两个内部态不出网（无对应动作，报出去只是噪音）：login_form（正常等人输入）、
 * unknown（加载中 / 认不出的页面）。
 */

/** 分类结果（内部态，含不出网的两个）。 */
export const STAGE = {
  TWO_FACTOR: "two_factor",
  CHECKPOINT: "checkpoint",
  PASSWORD_ERROR: "password_error",
  E2EE_PIN: "e2ee_pin",
  LOGIN_FORM: "login_form",
  UNKNOWN: "unknown",
};

// 显式二因子页面。/checkpoint/1501092823525282 是 FB「登录审批」检查点的固定 ID
// （输 6 位码那一屏），十余年未变，比 DOM 选择器稳得多。
const TWO_FACTOR_MARKERS = [
  "/two_step_verification/",
  "/authentication/",
  "/checkpoint/1501092823525282",
];

// 泛检查点：风控拦截、设备确认、账号受限都落这里。放在二因子判定之后，
// 因为二因子本身也是一种 checkpoint，先命中更具体的那条。
const CHECKPOINT_MARKER = "/checkpoint/";

// E2EE 设备 PIN / 密钥恢复（半死态重登时常见）。放在 checkpoint 之后、login_form 之前：
// 这些页 URL 常落在 messenger.com 根或 /t/ 下，靠 path/query 关键词 + 可选 pageText。
const E2EE_PIN_URL_MARKERS = [
  "encrypted_backup", "encryption_pin", "device_password", "secret_conversation",
  "e2ee", "key_change", "restore_chat",
];
// 裸 "e2ee" 标记的例外（P4 2026-08-15 生产实锤）：**普通加密线程** URL 就是
// /e2ee/t/<数字>——重启恢复落在上次打开的线程页时，URL 判据把「平平无奇的加密
// 会话」误分类成 e2ee_pin（boot 日志 stage=e2ee_pin 但页面零 PIN 浮层，
// tryAutoE2eePin 空转、坐席登录流看到误导性「请填 PIN」提示码）。纯线程路径
// 剔除 URL 判据；真 PIN 浮层叠在线程页上时靠 pageText 文案判据兜住（分类器
// 与 detectPinPrompt 共用 E2EE_PIN_TEXT_RE，后者另有输入框在场的 AND 约束）。
const PLAIN_E2EE_THREAD_RE = /\/e2ee\/t\/\d+/;
const E2EE_PIN_TEXT_RE =
  /输入.*PIN|Enter your PIN|encryption PIN|加密.*PIN|设备密码|device password|恢复.*加密|Restore.*encrypted/i;

// 登录表单族（含账密页、设备登录页、找回密码页）。认出来只是为了「不报警」——
// 停在这里是完全正常的等待态。
const LOGIN_FORM_MARKERS = ["/login", "login.php", "/device-based/", "/recover/"];

/**
 * 页面可见文本是否是 E2EE 恢复 PIN 提示（登录分类与运行期浮层检测共用同一判据，
 * 防两处词表漂移——auto-PIN 只在这个判据命中时才会碰键盘）。
 */
export function isE2eePinText(text) {
  return E2EE_PIN_TEXT_RE.test(String(text || ""));
}

/**
 * 按旁观信号判断登录页此刻处于哪一段。
 *
 * @param {object} sig
 * @param {string}  sig.url         page.url()
 * @param {boolean} sig.hasCUser    cookie c_user 是否已下发
 * @param {boolean} sig.hasXs       cookie xs 是否已下发
 * @param {boolean} sig.hasErrorBox 登录表单上是否挂着错误框
 * @param {string}  [sig.pageText]  可选：页面可见文本片段（E2EE PIN 文案兜底）
 * @param {boolean} [sig.hasLoginForm] 可选：DOM 上是否有账密输入框（input[name=email]/[name=pass]）。
 *                  表单中继关键信号：messenger.com 未登录时落在根路径 https://www.messenger.com/，
 *                  URL 不带 /login 标记，但页面就是登录表单——只靠 URL 标记会误判 unknown，
 *                  中继流据此永远停在 wait（2026-08-13 canary 实测捕获）。
 * @returns {string} STAGE.*
 */
export function classifyLoginPage(sig) {
  const url = String((sig && sig.url) || "").toLowerCase();
  const hasCUser = !!(sig && sig.hasCUser);
  const hasXs = !!(sig && sig.hasXs);
  const hasErrorBox = !!(sig && sig.hasErrorBox);
  const pageText = String((sig && sig.pageText) || "");
  const hasLoginForm = !!(sig && sig.hasLoginForm);

  if (TWO_FACTOR_MARKERS.some((m) => url.includes(m))) return STAGE.TWO_FACTOR;
  if (url.includes(CHECKPOINT_MARKER)) return STAGE.CHECKPOINT;
  // cookie 签名：FB 在密码校验通过后就下发 c_user，xs 要等整条认证走完。两者错位＝
  // 卡在第二因子。URL 认不出改版新页面时，这条仍然成立（不依赖任何页面结构）。
  if (hasCUser && !hasXs) return STAGE.TWO_FACTOR;
  // E2EE PIN：完整会话 cookie 已有，但仍卡在设备密钥页（半死态重登关键路径）。
  // 只豁免裸 "e2ee" 标记撞上纯加密线程路径（/e2ee/t/<数字>=正常会话页）的组合；
  // 具体标记（encryption_pin/encrypted_backup…）即使叠在线程 URL 查询串上仍算
  // 证据。线程页上的真 PIN 浮层另有 pageText 文案判据兜住。
  const urlPinHit = E2EE_PIN_URL_MARKERS.some((m) =>
    url.includes(m) && !(m === "e2ee" && PLAIN_E2EE_THREAD_RE.test(url)));
  if (urlPinHit || E2EE_PIN_TEXT_RE.test(pageText)) {
    return STAGE.E2EE_PIN;
  }
  // 错误框不限定 URL：messenger.com 根路径也直接渲染登录表单（URL 里没有 /login）。
  if (hasErrorBox) return STAGE.PASSWORD_ERROR;
  // URL 命中登录页族（原有判据，保持逐字节不变）。
  if (LOGIN_FORM_MARKERS.some((m) => url.includes(m))) return STAGE.LOGIN_FORM;
  // DOM 兜底：页面真有账密输入框且尚未完整登录（c_user+xs 未双全）→ 就是登录表单。
  // 这条专治 messenger.com 根路径登录页（URL 无 /login 标记）——headed 流靠人眼无所谓，
  // 但表单中继必须认出它才渲得出账密表单。login_form 非 actionable（actionableCode 回 ''），
  // 故对既有 headed 流的 hint_code / 失败归因零影响，只解锁中继流。
  if (hasLoginForm && !(hasCUser && hasXs)) return STAGE.LOGIN_FORM;
  return STAGE.UNKNOWN;
}

/**
 * 分类结果 → 对外原因码。只有「运维能据此做点什么」的段落出网；
 * 正常等待态返回空串（前端保持默认指引，不弹无谓提示）。
 */
export function actionableCode(stage) {
  switch (stage) {
    case STAGE.TWO_FACTOR:
    case STAGE.CHECKPOINT:
    case STAGE.PASSWORD_ERROR:
    case STAGE.E2EE_PIN:
      return stage;
    default:
      return "";
  }
}
