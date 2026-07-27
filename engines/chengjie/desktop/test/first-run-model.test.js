"use strict";

// 首启向导纯函数模型单测（P0-1 A2/A5，无框架，node 直跑）：node test/first-run-model.test.js
const assert = require("assert");
const {
  FR_STRINGS,
  frT,
  frBuildSteps,
  frAiPrefill,
  frValidateAiInput,
  frAiTestView,
  frAiSaveView,
  frResultView,
  frTrialView,
  frClaimValidate,
  frClaimResultView,
  frClaimPollPlan,
  frGiftView,
} = require("../renderer/first-run-model.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── 步骤编排：AI 已配置 → 只走基础步；未配置/后端不可达 → 三步 ──
ok("已配置只走基础步", frBuildSteps({ ok: true, configured: true }).join(",") === "basic");
ok("未配置走三步", frBuildSteps({ ok: true, configured: false }).join(",") === "basic,ai,result");
ok("后端不可达也给填 Key 机会", frBuildSteps(null).join(",") === "basic,ai,result");
ok("状态接口失败同上", frBuildSteps({ ok: false }).join(",") === "basic,ai,result");

// ── 体验额度收尾步（P2）：只在后端确认「真的有、真的是体验档、还没用尽」时追加 ──
//   这份额度默默生效，不说出来等于白送；但也绝不能凭空承诺一份不存在的额度。
const TRIAL_ON = { ok: true, visible: true, source: "local_trial", included: 10000,
  remaining: 8500, hours_left: 46.2, exceeded: false };
ok("有体验额度→追加收尾步",
  frBuildSteps({ ok: true, configured: true }, TRIAL_ON).join(",") === "basic,trial");
ok("三步流程也追加收尾步",
  frBuildSteps(null, TRIAL_ON).join(",") === "basic,ai,result,trial");
ok("拿不到额度状态→不加步骤", frBuildSteps(null, null).join(",") === "basic,ai,result");
ok("端点不可见→不加步骤",
  frBuildSteps(null, { ok: true, visible: false }).join(",") === "basic,ai,result");
ok("付费额度不进欢迎流",
  frBuildSteps(null, { ok: true, visible: true, source: "license", included: 3000000 })
    .join(",") === "basic,ai,result");
ok("已用尽不拿来当欢迎语",
  frBuildSteps(null, Object.assign({}, TRIAL_ON, { exceeded: true }))
    .join(",") === "basic,ai,result");

const tv = frTrialView(TRIAL_ON, "zh");
ok("展示剩余而非总量", tv.chars === 8500);
ok("剩余时长透传", Math.round(tv.hours) === 46);
ok("有文案", !!tv.title && !!tv.sub);
const tvEn = frTrialView(TRIAL_ON, "en");
ok("英文文案不同于中文", tvEn.title !== tv.title);
const tvNoRemain = frTrialView({ ok: true, visible: true, source: "local_trial",
  included: 10000, hours_left: null }, "zh");
ok("缺 remaining 回落 included", tvNoRemain.chars === 10000);
ok("缺时长 → null（UI 隐藏该格）", tvNoRemain.hours === null);
ok("空输入不炸", frTrialView(undefined).show === false);

// ── 预填：状态可用给回显值；不可用回落桌面种子默认（deepseek） ──
const pre1 = frAiPrefill({ ok: true, configured: false, base_url: "http://x/v1", model: "m1" });
ok("预填用后端回显", pre1.base_url === "http://x/v1" && pre1.model === "m1");
const pre2 = frAiPrefill(null);
ok("后端不可达回落默认 base_url", pre2.base_url === "https://api.deepseek.com");
ok("后端不可达回落默认 model", pre2.model === "deepseek-chat");
const pre3 = frAiPrefill({ ok: true, configured: true, api_key_masked: "sk-a…mnop" });
ok("已配置带打码 key", pre3.configured === true && pre3.api_key_masked === "sk-a…mnop");

// ── 输入校验：key 必填；base/model 可空 ──
ok("空 key 拒绝", frValidateAiInput({ api_key: "  " }).ok === false);
ok("空 key 错误码", frValidateAiInput({ api_key: "" }).err === "key_required");
ok("有 key 放行", frValidateAiInput({ api_key: "sk-1" }).ok === true);
ok("base/model 可空", frValidateAiInput({ api_key: "sk-1", base_url: "", model: "" }).ok === true);

// ── 测试连接视图 ──
ok("测试成功绿字", frAiTestView({ ok: true }, "zh").cls === "ok");
const tf = frAiTestView({ ok: false, msg: "HTTP 401" }, "zh");
ok("测试失败带原因", tf.cls === "err" && tf.text.indexOf("HTTP 401") >= 0);
ok("测试无响应也 err", frAiTestView(null, "zh").cls === "err");

// ── 保存视图（A5 绿灯语义：ai_ready 才 ready） ──
const sv1 = frAiSaveView({ ok: true, ai_ready: true }, "zh");
ok("保存+就绪 → ok/ready", sv1.cls === "ok" && sv1.ready === true);
const sv2 = frAiSaveView({ ok: true, ai_ready: false }, "zh");
ok("保存但未验证 → warn/未就绪", sv2.cls === "warn" && sv2.ready === false);
const sv3 = frAiSaveView({ ok: false, detail: "保存失败：x" }, "zh");
ok("保存失败 → err 带 detail", sv3.cls === "err" && sv3.text.indexOf("保存失败：x") >= 0);
ok("无响应 → err", frAiSaveView(null, "zh").cls === "err");

// ── 结果终屏 ──
const r1 = frResultView(sv1, "zh");
ok("就绪 → 绿灯终屏", r1.cls === "ok" && r1.title === FR_STRINGS.zh.result_ok_title);
const r2 = frResultView(sv2, "zh");
ok("未就绪 → 黄灯终屏", r2.cls === "warn" && r2.title === FR_STRINGS.zh.result_warn_title);
ok("null 保存视图安全", frResultView(null, "zh").cls === "warn");

// ── 注册领 7 天（P2）──
// 这一步的产品铁律是「随时可跳过」：领取失败只是提示，不能变成开机第一屏的拦路弹窗。
// 下面每条断言都在守这件事——任何失败态都必须 done=true（渲染出「开始使用」按钮）。

// 联系方式校验：宽松收，只挡空值与一两个字符
ok("空联系方式拒绝", frClaimValidate("").ok === false);
ok("纯空白拒绝", frClaimValidate("   ").ok === false);
ok("拒绝时给错误码", frClaimValidate("").err === "claim_need_contact");
ok("@handle 放行", frClaimValidate("@bob_2026").ok === true);
ok("裸 handle 放行", frClaimValidate("bob").ok === true);
ok("手机号放行", frClaimValidate("+86 138-0013-8000").ok === true);
ok("单字符拒绝", frClaimValidate("@a").ok === false);
ok("null 不炸", frClaimValidate(null).ok === false);

// claim/poll 响应 → 展示模型
const cvOk = frClaimResultView({ ok: true, activated: true }, "zh");
ok("激活 → ok 终态", cvOk.phase === "ok" && cvOk.done === true);
ok("激活后可继续领赠量", cvOk.canGift === true);
const cvWait = frClaimResultView({ ok: true, status: "pending" }, "zh");
ok("待签发是中间态不是错误", cvWait.phase === "waiting" && cvWait.done === false);
ok("待签发不给领赠量入口", cvWait.canGift === false);
const cvGone = frClaimResultView({ ok: true, trial_exhausted: true }, "zh");
ok("本机已用尽 → 独立终态", cvGone.phase === "exhausted" && cvGone.done === true);
ok("已用尽不给领赠量", cvGone.canGift === false);
ok("已用尽文案≠通用失败", cvGone.text !== frClaimResultView({ ok: false }, "zh").text);
const cvExpired = frClaimResultView({ ok: true, activate_error: "expired" }, "zh");
ok("过期授权按已用尽处理", cvExpired.phase === "exhausted");
const cvFp = frClaimResultView({ ok: false, error: "no_fingerprint" }, "zh");
ok("读不到机器码 → 专门文案", cvFp.text === FR_STRINGS.zh.claim_no_fp && cvFp.done === true);
const cvNet = frClaimResultView({ ok: false, error: "network" }, "zh");
ok("网络失败 → 专门文案", cvNet.text === FR_STRINGS.zh.claim_network);
ok("HTTP 错误归为网络类", frClaimResultView({ ok: false, error: "http_502" }, "zh").text
  === FR_STRINGS.zh.claim_network);
const cvUnknown = frClaimResultView({ ok: false, error: "weird_thing" }, "zh");
ok("未知错误带上原始码便于上报", cvUnknown.text.indexOf("weird_thing") >= 0);
ok("空响应不炸且可跳过", frClaimResultView(null, "zh").done === true);
ok("所有失败态都能走人",
  [cvFp, cvNet, cvUnknown, cvGone].every((v) => v.done === true));
ok("英文文案独立", frClaimResultView({ ok: false, error: "network" }, "en").text
  === FR_STRINGS.en.claim_network);

// 轮询编排：前几次快查，之后交给后端后台轮询并放用户走
ok("首次要再轮", frClaimPollPlan(0).again === true);
ok("轮询间隔 5s", frClaimPollPlan(0).delayMs === 5000);
ok("到上限就收手", frClaimPollPlan(6).again === false);
ok("收手时给「稍后自动生效」文案", frClaimPollPlan(6).giveUpKey === "claim_slow");
ok("上限可调", frClaimPollPlan(2, 2).again === false);

// 客服赠量绑定码
const gv = frGiftView({ ok: true, bind_code: "BC-1111-2222",
  telegram_url: "https://t.me/x?text=BC" }, "zh");
ok("出码", gv.ok === true && gv.code === "BC-1111-2222");
ok("出深链", gv.tgUrl.indexOf("https://t.me/") === 0);
ok("取码失败给提示", frGiftView({ ok: false }, "zh").ok === false);
ok("有 ok 无 code 也算失败", frGiftView({ ok: true }, "zh").ok === false);
ok("空响应不炸", frGiftView(null, "zh").ok === false);
ok("无深链但有码仍算成功",
  frGiftView({ ok: true, bind_code: "BC-A" }, "zh").ok === true);

// ── i18n：zh/en 键齐 + 回落 ──
const zhKeys = Object.keys(FR_STRINGS.zh).sort().join("|");
const enKeys = Object.keys(FR_STRINGS.en).sort().join("|");
ok("zh/en 键集合一致", zhKeys === enKeys);
ok("en 取英文", frT("en", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("未知语言回落 zh", frT("fr", "btn_finish") === FR_STRINGS.zh.btn_finish);
ok("未知键回显键名", frT("zh", "nope_x") === "nope_x");

console.log(`first-run-model.test.js: ${pass} passed`);
