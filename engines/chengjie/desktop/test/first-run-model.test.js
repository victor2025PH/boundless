"use strict";

// 首启向导纯函数模型单测（P0-1 A2/A5，无框架，node 直跑）：node test/first-run-model.test.js
const assert = require("assert");
const {
  FR_STRINGS,
  FR_FUNNEL_EVENTS,
  FR_CLAIM_CHARS,
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
  frShouldShowWizard,
  frWelcomeView,
  frCelebrateView,
  frFeatureList,
  frQuotaCompareView,
  frNextSteps,
  frFormatChars,
} = require("../renderer/first-run-model.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── 字符数展示跟语言走（原来一律「1w」，英文界面也是「1w」= 看不懂）──
ok("中文万位", frFormatChars(10000, "zh") === "1 万");
ok("中文带小数", frFormatChars(25000, "zh") === "2.5 万");
ok("中文小于万给千分位", frFormatChars(8000, "zh") === "8,000");
ok("英文一律千分位", frFormatChars(10000, "en") === "10,000");
ok("英文不出现万", frFormatChars(25000, "en").indexOf("万") < 0);
ok("英文不出现 w 缩写", /w/i.test(frFormatChars(100000, "en")) === false);
ok("空值不炸", frFormatChars(null, "zh") === "0");
ok("语言留空按中文", frFormatChars(10000, "") === "1 万");

// ── 弹不弹：默认只弹一次，--first-run 可强制重看；config.onboarding 也是权威完成态 ──
ok("首次弹", frShouldShowWizard(false, false) === true);
ok("弹过就不再弹", frShouldShowWizard(true, false) === false);
ok("强制时无视标记", frShouldShowWizard(true, true) === true);
ok("强制且没弹过照样弹", frShouldShowWizard(false, true) === true);
ok("config 已完成也不弹", frShouldShowWizard(false, false, true) === false);
ok("localStorage 或 config 任一完成即不弹", frShouldShowWizard(true, false, false) === false);
ok("强制无视 config 完成态", frShouldShowWizard(true, true, true) === true);

// ── 步骤编排：AI 已配置 → 只走基础步；未配置/后端不可达 → 三步 ──
ok("已配置只走基础步", frBuildSteps({ ok: true, configured: true }).join(",") === "basic");
ok("未配置走三步", frBuildSteps({ ok: true, configured: false }).join(",") === "basic,ai,result");
ok("后端不可达也给填 Key 机会", frBuildSteps(null).join(",") === "basic,ai,result");
ok("状态接口失败同上", frBuildSteps({ ok: false }).join(",") === "basic,ai,result");

// ── 托管版（客户成品）：固定 welcome→claim→celebrate，永不出现 ai/result ──
//   不靠「此刻 AI 配没配好」猜；领取失败可跳过，故无额度状态也照样进漏斗。
ok("托管版即使 AI 未配置也不出 ai/result 步",
  frBuildSteps({ ok: false }, null, { managed: true }).join(",") === "welcome,claim,celebrate");
ok("托管版 AI 后端不可达也不出配置步",
  frBuildSteps(null, null, { managed: true }).join(",") === "welcome,claim,celebrate");
ok("托管版有体验额度仍是固定三屏(额度并入欢迎屏)",
  frBuildSteps(null, TRIAL_ON_EARLY(), { managed: true }).join(",") === "welcome,claim,celebrate");
ok("非托管保持旧行为(未配置走三步)",
  frBuildSteps({ ok: false }, null, { managed: false }).join(",") === "basic,ai,result");
ok("缺 opts 视为非托管(向后兼容)",
  frBuildSteps({ ok: false }, null).join(",") === "basic,ai,result");
function TRIAL_ON_EARLY() {
  return { ok: true, visible: true, source: "local_trial", included: 10000,
    remaining: 8500, hours_left: 46.2, exceeded: false };
}

// ── 托管欢迎 / 庆祝纯模型 ──
const wv = frWelcomeView(TRIAL_ON_EARLY(), "zh");
ok("欢迎屏有额度时展示数字", wv.showQuota === true && wv.chars === 8500);
ok("欢迎屏 CTA 是领取完整版", wv.cta === FR_STRINGS.zh.btn_claim_start);
ok("欢迎屏价值钩子非空", !!wv.sub && wv.sub.indexOf("不用") >= 0);
ok("无额度状态不展示数字格", frWelcomeView(null, "zh").showQuota === false);
const cel1 = frCelebrateView({ claimed: true }, "zh");
ok("已领取→激活文案", cel1.sub === FR_STRINGS.zh.celebrate_sub_claimed);
const cel2 = frCelebrateView({ claimed: false, giftShown: true }, "zh");
ok("看过赠量码→赠量文案", cel2.sub === FR_STRINGS.zh.celebrate_sub_gift);
const cel3 = frCelebrateView({}, "zh");
ok("跳过领取→体验就绪文案", cel3.sub === FR_STRINGS.zh.celebrate_sub_skipped);
ok("庆祝 CTA 进工作台", cel3.cta === FR_STRINGS.zh.btn_enter);
ok("英文庆祝文案独立",
  frCelebrateView({ claimed: true }, "en").sub === FR_STRINGS.en.celebrate_sub_claimed);

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

// ── P0 首屏说明+引导（2026-08-21）：能力三点式 / 10 倍对比 / 三步引导 / 回门 ──
const fl = frFeatureList("zh");
ok("能力三点式恒 3 条", fl.length === 3);
ok("每条有图标/标题/描述", fl.every((f) => !!f.icon && !!f.t && !!f.d));
ok("能力文案英文独立", frFeatureList("en")[0].t !== fl[0].t);

const cmp = frQuotaCompareView(TRIAL_ON, "zh");
ok("体验档可见才出对比", cmp.show === true);
ok("对比左格=本机剩余真值", cmp.now.chars === "8,500");
ok("对比右格=注册领取营销数字", cmp.claim.chars === "100 万");
ok("英文右格千分位", frQuotaCompareView(TRIAL_ON, "en").claim.chars === "1,000,000");
ok("营销数字常量=100万", FR_CLAIM_CHARS === 1000000);
ok("额度状态探不到不摆数字", frQuotaCompareView(null, "zh").show === false);
ok("已用尽不进对比",
  frQuotaCompareView(Object.assign({}, TRIAL_ON, { exceeded: true }), "zh").show === false);
ok("付费额度不进对比", frQuotaCompareView(
  { ok: true, visible: true, source: "license", included: 3000000 }, "zh").show === false);

const ns = frNextSteps("zh");
ok("三步引导恒 3 条", ns.length === 3);
ok("每步有序号/标题/描述", ns.every((s) => !!s.n && !!s.t && !!s.d));
ok("三步引导英文独立", frNextSteps("en")[0].t !== ns[0].t);

// 就绪屏扩展：未领取给「回去领取」门（bind-code 需先领取，绑定码入口对 skip 必败）
const celBack = frCelebrateView({ claimed: false }, "zh");
ok("未领取→就绪屏给回门",
  celBack.showClaimBack === true && celBack.backCta === FR_STRINGS.zh.btn_back_claim);
ok("已领取→不给回门", frCelebrateView({ claimed: true }, "zh").showClaimBack === false);
ok("就绪屏带三步引导", celBack.steps.length === 3 && !!celBack.stepsTitle);

// ── P1 步骤指示器三态（done/current/todo）──
const { frStepDotsView } = require("../renderer/first-run-model.js");
const sd = frStepDotsView(["welcome", "claim", "celebrate"], "claim");
ok("当前步之前=done", sd[0].state === "done");
ok("当前步=current", sd[1].state === "current");
ok("当前步之后=todo", sd[2].state === "todo");
ok("首步激活时无 done",
  frStepDotsView(["welcome", "claim"], "welcome").every((s, i) => (i === 0 ? s.state === "current" : s.state === "todo")));
ok("末步激活时其余全 done",
  frStepDotsView(["a", "b", "c"], "c").slice(0, 2).every((s) => s.state === "done"));
ok("activeId 不存在→全 todo（异常态不装完成）",
  frStepDotsView(["a", "b"], "zzz").every((s) => s.state === "todo"));
ok("空 steps 不炸", frStepDotsView([], "x").length === 0);
ok("非数组不炸", frStepDotsView(null, "x").length === 0);

// ── P1 渲染层源码契约（品牌 token 桥接 + 无障碍开关；扫 first-run.js 源文本）──
// 品牌色单一事实源=platform/brand/brand.css（index.html 已加载）；first-run.js 内联
// 样式必须经 var(--bl-*, 字面量兜底) 消费。这里钉两头：①渲染层真的在用这些 token；
// ②token 名在 brand.css 里真实存在（防「引用了不存在的变量→恒走兜底」静默漂移）。
const fs = require("fs");
const path = require("path");
const frSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "first-run.js"), "utf8");
["--bl-gradient-brand", "--bl-ink-700", "--bl-growth-500",
  "--bl-brand-cyan", "--bl-brand-violet"].forEach(function (tok) {
  ok("渲染层引用品牌 token " + tok, frSrc.indexOf("var(" + tok) >= 0);
});
ok("动画带 reduced-motion 关停", frSrc.indexOf("prefers-reduced-motion") >= 0);
ok("步骤指示器接模型三态", frSrc.indexOf("frStepDotsView") >= 0);
ok("就绪屏对勾有弹入动画类", frSrc.indexOf("fr-anim-pop") >= 0);
const brandCssPath = path.join(__dirname, "..", "..", "..", "..", "platform", "brand", "brand.css");
if (fs.existsSync(brandCssPath)) {
  const brandCss = fs.readFileSync(brandCssPath, "utf8");
  ["--bl-gradient-brand", "--bl-ink-700", "--bl-growth-500",
    "--bl-brand-cyan", "--bl-brand-violet"].forEach(function (tok) {
    ok("brand.css 存在 token " + tok, brandCss.indexOf(tok + ":") >= 0);
  });
  // 渐变兜底字面量的七个色标必须与 SSOT 逐一吻合（兜底=陈旧包时的视觉，不许漂）
  ["#00b0f0", "#1e6bf0", "#7a3bf5", "#d030f0", "#f0509a", "#f07800", "#f0a010"]
    .forEach(function (hex) {
      ok("渐变兜底色标在 SSOT 中 " + hex, brandCss.toLowerCase().indexOf(hex) >= 0);
      ok("渲染层兜底含色标 " + hex, frSrc.toLowerCase().indexOf(hex) >= 0);
    });
} else {
  console.log("  .. platform/brand/brand.css 不在（非完整仓布局），SSOT 比对跳过");
}

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

// ── 漏斗事件白名单：与后端 trial_claim_client.FUNNEL_EVENTS 同口径 ──
//   first-run.js 只从这张表取名——拼错事件名在这里就红，而不是线上数据悄悄丢。
ok("漏斗事件表齐全", FR_FUNNEL_EVENTS.join(",")
  === "welcome,claim_submit,claim_ok,claim_skip,gift_open,done,invite_share,"
    + "invite_open,claim_back,claim_banner");
ok("漏斗事件名全小写下划线", FR_FUNNEL_EVENTS.every(function (e) {
  return /^[a-z_]+$/.test(e);
}));

// ── i18n：zh/en 键齐 + 回落 ──
const zhKeys = Object.keys(FR_STRINGS.zh).sort().join("|");
const enKeys = Object.keys(FR_STRINGS.en).sort().join("|");
ok("zh/en 键集合一致", zhKeys === enKeys);
ok("en 取英文", frT("en", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("未知语言回落 zh", frT("fr", "btn_finish") === FR_STRINGS.zh.btn_finish);
ok("未知键回显键名", frT("zh", "nope_x") === "nope_x");
// 扩展语白名单（vi/th/id）→ en 底（与壳 shellLang/SS 同向；跟随系统/真未知仍 zh）
ok("扩展语 vi 回落 en", frT("vi", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("扩展语 th 回落 en", frT("th", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("扩展语 id 回落 en", frT("id", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("跟随系统仍回落 zh", frT("", "btn_finish") === FR_STRINGS.zh.btn_finish);
// 繁体 zh_hant → zh 底（向导词典无繁体；简体可读，英文才是断崖）
ok("繁体 zh_hant 回落 zh", frT("zh_hant", "btn_finish") === FR_STRINGS.zh.btn_finish);
ok("繁体数字单位用萬", frFormatChars(1000000, "zh_hant") === "100 萬");
ok("简体数字单位仍万", frFormatChars(1000000, "zh") === "100 万");

// ── 系统语言映射（「跟随系统」真跟随；与 main.js::shellLangFromTag 同口径）──
const frSysLang = require("../renderer/first-run-model.js").frSysLang;
[["zh-CN", "zh"], ["zh", "zh"], ["zh-Hans-SG", "zh"],
 ["zh-TW", "zh_hant"], ["zh-HK", "zh_hant"], ["zh-Hant-TW", "zh_hant"], ["zh-MO", "zh_hant"],
 ["en-US", "en"], ["en", "en"], ["vi-VN", "vi"], ["th-TH", "th"], ["id-ID", "id"],
 ["ja-JP", ""], ["", ""], [null, ""]]
  .forEach(([raw, want]) => {
    ok(`frSysLang(${JSON.stringify(raw)}) === ${JSON.stringify(want)}`,
      frSysLang(raw) === want);
  });

console.log(`first-run-model.test.js: ${pass} passed`);
