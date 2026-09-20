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
  ACCOUNT_LOCKED: "account_locked",
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
// B64 续（2026-08-23 23:55 实录，117 relay-submit 后 0.7s）：FB 把可疑登录押送到
// `/login/checkpoint_interstitial/?next=…%2Fcheckpoint%2Fstart…`——旧判据只认带尾
// 斜杠的 `/checkpoint/`，interstitial 变体（下划线接续）不命中，反而被 LOGIN_FORM
// 的 `/login` 前缀截胡 → 提交后界面对「已进检查点」全盲，用户看到的是「点了没反应」。
// 判据放宽为无尾斜杠前缀 `/checkpoint`（FB 路径里该词根只用于安全检查点语义），
// 另对 URL 做一次 decode 再匹配（checkpoint 藏在 next= 编码参数里的形态）。
const CHECKPOINT_MARKER = "/checkpoint";

// B64（2026-08-23 `_316`/`_317`）：「确认您是真人」机器人验证可以渲染在**非**
// /checkpoint/ 路径上（登录过渡页/白页上的一小块验证组件）——URL 判据漏网时靠
// 可见文案兜底。它本质就是检查点的一种：未通过前 FB 不会发 2FA 码，前端拿到
// checkpoint 提示码才能给「先过验证再等码」的实情指引（而不是让人干等）。
// 词形刻意收窄到 robot-check 专有句式，不收「security check」这类泛词（2FA 页
// 的说明文字也常含它，会把「正常等码」误报成检查点）。
const ROBOT_CHECK_TEXT_RE =
  /确认您是真人|確認您是真人|确认你是真人|证明您是真人|confirm\s+(that\s+)?you'?re\s+(a\s+)?human|prove\s+(that\s+)?you'?re\s+(a\s+)?human|confirm\s+that\s+you\s+are\s+human/i;

// P1（实施64 B64 二期）：「账号被临时锁定 / 尝试过多」与泛检查点分开报——两者出路完全
// 不同（锁定＝等冷却或去 facebook.com/login/identify 申诉，泛检查点＝当场过验证），混成
// 一个码用户只会拿着「过验证」的指引去撞一堵「稍后再试」的墙。词形收窄到 FB 实际句式，
// 泛词（如单独的 try again later）刻意不收防误报。
const LOCKED_TEXT_RE =
  /you.{0,3}re\s+temporarily\s+(blocked|locked)|account\s+(is\s+)?temporarily\s+locked|too\s+many\s+attempts|try\s+again\s+later.{0,40}too\s+many|你已被暂时封锁|帳號已被暫時封鎖|账[号户]已?被?暂时锁定|尝试次数过多|嘗試次數過多|操作过于频繁/i;

// B64 三期（2026-08-24，「手机确认后卡死」余波）：检查点内的「等手机确认」子味——
// 「请验证你的 Facebook 帐户」入口页 / 「查看通知/在手机上批准」等页面，用户的正确动作
// 是「去手机 App 确认 → 回来点继续」，与机器人验证（必须盯着画面过验证）叙事完全不同。
// 词形保守收窄到确认类专有句式；泛词（verify/confirm 单独出现）刻意不收——误报会把
// 「必须看画面」的用户引去手机上空找通知。
const DEVICE_CONFIRM_TEXT_RE =
  /请验证你的.{0,10}[帐账帳]户|請驗證你的.{0,10}帳戶|在手机上(确认|批准)|在手機上(確認|批准)|查看.{0,6}通知|我们已.{0,16}发送.{0,10}通知|check\s+your\s+notifications|approve\s+(this\s+|your\s+)?log-?\s?in|we\s+sent\s+(you\s+)?a\s+notification|approve\s+from\s+another\s+device|waiting\s+for\s+(your\s+)?approval/i;

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

// B99（实施68 P1-9 残留 ⑤）：「你已被暂时阻止 / temporarily blocked」类临时风控页。
// 0825 实锤：PIN 态反复重试风暴后 FB 对该号弹临时封锁。词表刻意保守（宁漏勿误——
// 误判=好端端的号被冻 2h）：只认「temporarily/暂时 × blocked/restricted/阻止/封锁」
// 的强组合文案，不认泛泛的 "try again later"。
const TEMP_BLOCKED_TEXT_RE = new RegExp(
  "(you'?re?\\s+temporarily\\s+blocked|temporarily\\s+blocked|"
  + "temporarily\\s+restricted|"
  + "(你|您)已?被?暂时(阻止|封锁|限制)|暂时(封锁|阻止)了(你|您)|"
  + "(你|您)已?被?暫時(阻止|封鎖|限制))", "i");

/** 页面可见文本是否命中「临时封锁」风控提示（发送闸/ban_signal 共用判据）。 */
export function isTemporarilyBlockedText(text) {
  return TEMP_BLOCKED_TEXT_RE.test(String(text || ""));
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
  // 编码变体展开（checkpoint 常藏在 next=%2Fcheckpoint%2Fstart 里）；坏编码不抛。
  let urlDecoded = url;
  try { urlDecoded = decodeURIComponent(url).toLowerCase(); } catch (_) { /* keep raw */ }

  if (TWO_FACTOR_MARKERS.some((m) => url.includes(m))) return STAGE.TWO_FACTOR;
  // 锁定文案优先于泛检查点：锁定页多半也带 /checkpoint URL，但出路完全不同
  // （等冷却/申诉 vs 当场过验证）——更具体的判据先走。
  if (!(hasCUser && hasXs) && LOCKED_TEXT_RE.test(pageText)) return STAGE.ACCOUNT_LOCKED;
  if (url.includes(CHECKPOINT_MARKER) || urlDecoded.includes(CHECKPOINT_MARKER)) {
    return STAGE.CHECKPOINT;
  }
  // B64：页面可见「确认您是真人」＝活的机器人验证挡在面前——比 cookie 推断
  // （c_user 有 / xs 无 ⇒「等第二因子」）更接近真相：这道验证不过，2FA 码根本
  // 不会发。只在未完整登录时判（登录后的页面正文提到这句话不构成证据）。
  if (!(hasCUser && hasXs) && ROBOT_CHECK_TEXT_RE.test(pageText)) return STAGE.CHECKPOINT;
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
 * 检查点子味（B64 三期）：stage=CHECKPOINT 时进一步判「等手机确认」还是泛检查点。
 * **刻意不进 STAGE 枚举**：hint_code / reason_code / Python 漏斗契约全部不动（窗口期
 * 失败归因仍是 checkpoint），子味只经 relay-step 的 code 细化前端叙事（手机主视觉 +
 * 三步清单 vs 截图为主）。机器人验证显式排除——那类页用户必须看着画面过验证，
 * 把他引去手机上空找通知是指错路。
 *
 * @param {string} pageText 页面可见文本片段（与 classifyLoginPage 同源）
 * @returns {"device_confirm"|""}
 */
export function checkpointFlavor(pageText) {
  const t = String(pageText || "");
  if (!t) return "";
  if (ROBOT_CHECK_TEXT_RE.test(t)) return "";
  if (DEVICE_CONFIRM_TEXT_RE.test(t)) return "device_confirm";
  return "";
}

/**
 * 分类结果 → 对外原因码。只有「运维能据此做点什么」的段落出网；
 * 正常等待态返回空串（前端保持默认指引，不弹无谓提示）。
 */
export function actionableCode(stage) {
  switch (stage) {
    case STAGE.TWO_FACTOR:
    case STAGE.CHECKPOINT:
    case STAGE.ACCOUNT_LOCKED:
    case STAGE.PASSWORD_ERROR:
    case STAGE.E2EE_PIN:
      return stage;
    default:
      return "";
  }
}
