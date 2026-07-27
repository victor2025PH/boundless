"use strict";

// 首启向导纯函数模型（P0-1 A2/A5）：步骤编排 / AI 状态预填 / 测试与保存结果视图。
// 双模式：浏览器经 <script> 加载后挂全局（first-run.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/first-run-model.test.js 单测）。
//
// 设计：不碰 DOM / IPC——所有 I/O 由 first-run.js 完成，这里只做「响应 → 展示模型」映射，
// 保证向导流程可在无 Electron 环境下回归。

var FR_STRINGS = {
  zh: {
    ai_title: "配置 AI 大模型（翻译 / 智能回复引擎）",
    ai_sub: "只需一个 OpenAI 兼容 API Key（如 DeepSeek）。跳过则翻译暂不可用，之后可在后台「接入向导」补配。",
    ai_key_label: "AI API Key",
    ai_base_label: "接口地址（base_url）",
    ai_model_label: "模型",
    btn_test: "测试连接",
    btn_save: "保存并继续",
    btn_skip: "跳过",
    btn_back: "上一步",
    btn_finish: "完成并进入",
    testing: "正在连接 AI 服务…",
    test_ok: "连接成功，Key 有效",
    test_fail: "连接失败",
    saving: "正在保存…",
    save_fail: "保存失败",
    ready: "翻译就绪 ✓ 已保存并即时生效",
    saved_not_ready: "已保存，但连接自检未通过：请核对 Key / 接口地址 / 网络，稍后可在后台「接入向导」重试",
    key_required: "API Key 不能为空",
    backend_wait: "本地后台服务还在启动，请稍候几秒再试",
    result_ok_title: "一切就绪",
    result_ok_sub: "AI 已连通，翻译与智能回复可直接使用。",
    result_warn_title: "还差一步",
    result_warn_sub: "AI Key 已保存但未验证通过。进入后可在后台「接入向导 → AI 大模型」重测。",
    already_configured: "已检测到 AI 配置，无需重复填写。",
    trial_title: "已为你开启体验额度",
    trial_sub: "装完即可直接用，无需注册。额度与剩余时间随时可在顶栏徽章和「会员中心」查看。",
    trial_chars: "可用字符",
    trial_hours: "有效时长",
    trial_hours_unit: "小时",
    btn_start: "开始使用",
    // ── 注册领 7 天（P2）──
    claim_title: "留个联系方式，免费领 7 天完整版",
    // 「2.5 万」而非「25000」：上方额度格用 frFormatChars 出的是「1 万」，
    // 同一屏两种数字写法看着像两个人写的（视觉验收所见）。
    claim_sub: "7 天不限渠道 · 2.5 万字符，绑定本机使用。填 Telegram 用户名或手机号即可，不用注册账号、不填邮箱。",
    claim_ph: "@你的Telegram用户名 或 手机号",
    btn_claim: "免费领取",
    btn_skip_claim: "先不领，直接开始",
    claim_need_contact: "填一下联系方式，客服才能找到你",
    claim_sending: "正在提交…",
    claim_waiting: "已提交，正在签发…",
    claim_slow: "已提交。签发通常几十秒，好了会自动生效——你可以先开始使用。",
    claim_ok: "已激活：7 天完整版 · 25000 字符",
    claim_fail: "领取失败",
    claim_no_fp: "读不到本机标识，无法绑定授权。可先直接开始使用，稍后在「会员中心」重试。",
    claim_network: "连不上服务器。可先直接开始使用，稍后在「会员中心」重试。",
    claim_exhausted: "这台机器的免费试用已经用过了。可在「会员中心」购买，或联系客服。",
    // ── 加客服领 10 万字符 ──
    gift_title: "再领 10 万字符",
    gift_sub: "把下面这串码发给客服，核销后额度自动到账——不用你再回来操作。",
    btn_gift: "领 10 万字符",
    btn_open_tg: "打开 Telegram 发给客服",
    btn_open_wa: "用 WhatsApp 发给客服",
    btn_copy: "复制",
    copied: "已复制",
    gift_fail: "取码失败，稍后可在「会员中心」再试",
    gift_done: "已到账 10 万字符",
  },
  en: {
    ai_title: "Set up the AI model (translation / smart replies)",
    ai_sub: "One OpenAI-compatible API key (e.g. DeepSeek) is enough. Skip for now and translation stays off until configured in the admin setup wizard.",
    ai_key_label: "AI API Key",
    ai_base_label: "Endpoint (base_url)",
    ai_model_label: "Model",
    btn_test: "Test connection",
    btn_save: "Save & continue",
    btn_skip: "Skip",
    btn_back: "Back",
    btn_finish: "Finish",
    testing: "Connecting to AI service…",
    test_ok: "Connected — key works",
    test_fail: "Connection failed",
    saving: "Saving…",
    save_fail: "Save failed",
    ready: "Translation ready ✓ — saved and live",
    saved_not_ready: "Saved, but the connection check failed: verify key / endpoint / network. You can retest in the admin setup wizard.",
    key_required: "API Key is required",
    backend_wait: "Local backend is still starting — try again in a few seconds",
    result_ok_title: "All set",
    result_ok_sub: "AI is connected. Translation and smart replies work out of the box.",
    result_warn_title: "One more step",
    result_warn_sub: "The AI key is saved but not verified. Retest later in admin → setup wizard → AI model.",
    already_configured: "An AI configuration was detected — nothing to fill in.",
    trial_title: "Your starter allowance is ready",
    trial_sub: "Works right after install — no signup needed. You can check the remaining "
      + "quota and time any time from the top-bar badge or the membership center.",
    trial_chars: "Characters",
    trial_hours: "Valid for",
    trial_hours_unit: "hours",
    btn_start: "Start using",
    claim_title: "Leave a contact, get 7 days free",
    claim_sub: "7 days, all channels, 25,000 characters — bound to this machine. "
      + "A Telegram handle or phone number is enough; no account, no email.",
    claim_ph: "@your_telegram or phone number",
    btn_claim: "Claim for free",
    btn_skip_claim: "Skip, just start",
    claim_need_contact: "Add a contact so support can reach you",
    claim_sending: "Submitting…",
    claim_waiting: "Submitted, issuing…",
    claim_slow: "Submitted. Issuing usually takes under a minute and activates itself — "
      + "feel free to start using the app.",
    claim_ok: "Activated: 7-day full version · 25,000 characters",
    claim_fail: "Claim failed",
    claim_no_fp: "Can't read this machine's ID, so the licence can't be bound. "
      + "Start using the app for now and retry from the membership center.",
    claim_network: "Can't reach the server. Start using the app for now and retry "
      + "from the membership center.",
    claim_exhausted: "This machine has already used its free trial. You can purchase "
      + "from the membership center or contact support.",
    gift_title: "Get 100,000 more characters",
    gift_sub: "Send the code below to support. Once they redeem it the quota lands "
      + "automatically — you don't have to come back.",
    btn_gift: "Get 100,000 characters",
    btn_open_tg: "Open Telegram and message support",
    btn_open_wa: "Message support on WhatsApp",
    btn_copy: "Copy",
    copied: "Copied",
    gift_fail: "Couldn't get a code — retry later from the membership center",
    gift_done: "100,000 characters credited",
  },
};

function frT(lang, key) {
  var d = FR_STRINGS[lang === "en" ? "en" : "zh"] || FR_STRINGS.zh;
  return d[key] != null ? d[key] : key;
}

// 字符数展示。原实现一律缩成「1w / 2.5w」——那是中文口语写法，**英文界面照样显示
// `1w`**，对英文用户就是个不明所以的字符串（视觉验收时抓到）。改为跟语言走：
// 中文用「万」，英文用千分位。语言判定与 frT 同口径（只有显式 en 算英文）。
function frFormatChars(n, lang) {
  var v = Math.max(0, Math.round(Number(n) || 0));
  if (lang === "en") return v.toLocaleString("en-US");
  if (v >= 10000) {
    var w = v / 10000;
    return (w >= 100 ? w.toFixed(0) : w.toFixed(1).replace(/\.0$/, "")) + " 万";
  }
  return v.toLocaleString("zh-CN");
}

// 步骤编排：AI 已配置（升级/重装保留数据目录）→ 只走基础步；未配置 → 基础 + AI + 结果。
// aiStatus = GET /api/setup/ai 响应（或 null/失败 → 视为未配置，让用户有机会填）。
//
// trialStatus = GET /api/workspace/quota 响应。体验额度真的开着时**追加最后一步**，
// 把「你有多少额度、能用多久」当收尾告知——这份额度现在是默默生效的，用户压根不知道
// 自己有；不说出来等于白送。拿不到状态 → 不加步骤（宁可不说，也不能凭空承诺额度）。
function frBuildSteps(aiStatus, trialStatus) {
  var configured = !!(aiStatus && aiStatus.ok && aiStatus.configured);
  var steps = configured ? ["basic"] : ["basic", "ai", "result"];
  if (frTrialView(trialStatus).show) steps.push("trial");
  return steps;
}

// 体验额度展示模型。只在「后端确认可见 + 确实是体验档 + 还没用尽」时 show——
// 付费额度不该出现在首启欢迎流里，用尽/过期更不该拿来当欢迎语。
function frTrialView(trialStatus, lang) {
  var d = trialStatus || {};
  var show = !!(d.ok && d.visible && d.source === "local_trial" && !d.exceeded);
  return {
    show: show,
    title: frT(lang, "trial_title"),
    sub: frT(lang, "trial_sub"),
    chars: Number(d.remaining != null ? d.remaining : (d.included || 0)) || 0,
    hours: (d.hours_left == null ? null : Number(d.hours_left)),
  };
}

// ── 注册领 7 天（P2）纯模型 ────────────────────────────────────────────────
//
// 一条硬约束贯穿下面几个函数：**领取失败绝不能把人卡在开机第一屏**。体验档已经
// 在跑，用户此刻什么都不做也能用；所以任何错误都渲染成「可跳过的提示」而不是拦路弹窗。

// 联系方式校验。刻意宽松——这不是注册表单，只是「客服怎么找到你」：
// @handle / handle / 手机号都收，只挡空值和明显不是联系方式的一两个字符。
function frClaimValidate(contact) {
  var s = String(contact == null ? "" : contact).trim();
  if (!s) return { ok: false, err: "claim_need_contact" };
  var core = s.replace(/^@+/, "").replace(/[\s\-()]/g, "");
  if (core.replace(/^\+/, "").length < 3) return { ok: false, err: "claim_need_contact" };
  return { ok: true, err: "" };
}

// claim / poll 响应 → 展示模型。
// phase: sending | waiting | ok | exhausted | fail —— UI 据此决定按钮组与配色。
// 「已激活」与「已用尽」是两种终态：前者继续引导领赠量，后者引导购买，绝不混为一谈。
function frClaimResultView(resp, lang) {
  var d = resp || {};
  if (d.activated) {
    return { phase: "ok", cls: "ok", text: frT(lang, "claim_ok"), done: true, canGift: true };
  }
  if (d.trial_exhausted || d.exhausted || d.activate_error === "expired") {
    return { phase: "exhausted", cls: "warn", text: frT(lang, "claim_exhausted"),
      done: true, canGift: false };
  }
  if (d.ok) {
    // 建单/轮询成功但还没签发下来——这是最常见的中间态，不是错误。
    return { phase: "waiting", cls: "info", text: frT(lang, "claim_waiting"),
      done: false, canGift: false };
  }
  var err = String(d.error || d.activate_error || "");
  var key = err === "no_fingerprint" ? "claim_no_fp"
    : (err === "network" || err.indexOf("http_") === 0) ? "claim_network"
      : "claim_fail";
  var extra = (key === "claim_fail" && err) ? "：" + err : "";
  return { phase: "fail", cls: "err", text: frT(lang, key) + extra, done: true, canGift: false };
}

// 轮询编排：给定已轮询次数，回答「还要不要再轮、下次隔多久」。
// 前 6 次 5 秒一轮（覆盖厂商机 cron 的常见延迟），之后就交给**后端后台轮询**兜底
// 并让用户走人——把开机第一屏变成进度条守望是最差的体验。
function frClaimPollPlan(attempt, maxAttempts) {
  var max = maxAttempts == null ? 6 : maxAttempts;
  var n = Number(attempt) || 0;
  if (n >= max) return { again: false, delayMs: 0, giveUpKey: "claim_slow" };
  return { again: true, delayMs: 5000, giveUpKey: "" };
}

// bind-code 响应 → 展示模型。深链拿不到也不算失败（码本身就能手发给客服）。
function frGiftView(resp, lang) {
  var d = resp || {};
  if (!d.ok || !d.bind_code) {
    return { ok: false, code: "", tgUrl: "", text: frT(lang, "gift_fail") };
  }
  return {
    ok: true,
    code: String(d.bind_code),
    tgUrl: String(d.telegram_url || ""),
    waUrl: String(d.whatsapp_url || ""),
    text: frT(lang, "gift_sub"),
  };
}

// AI 步预填：后端不可达/未配置时给桌面种子同款默认（deepseek），保证表单可直接填。
function frAiPrefill(aiStatus) {
  var st = (aiStatus && aiStatus.ok) ? aiStatus : {};
  return {
    base_url: st.base_url || "https://api.deepseek.com",
    model: st.model || "deepseek-chat",
    configured: !!st.configured,
    api_key_masked: st.api_key_masked || "",
  };
}

// 输入校验（提交前）：key 必填；base_url/model 可空（后端回落已存值/默认）。
function frValidateAiInput(vals) {
  var key = String((vals && vals.api_key) || "").trim();
  if (!key) return { ok: false, err: "key_required" };
  return { ok: true, err: "" };
}

// POST /api/setup/test-ai 响应 → 展示模型 {cls, text}
function frAiTestView(resp, lang) {
  if (resp && resp.ok) return { cls: "ok", text: frT(lang, "test_ok") };
  var extra = resp && (resp.msg || resp.detail) ? String(resp.msg || resp.detail) : "";
  return { cls: "err", text: frT(lang, "test_fail") + (extra ? ": " + extra : "") };
}

// POST /api/setup/ai-key 响应 → 展示模型 {cls, ready, text}
// ready=true 才算「翻译就绪」绿灯（后端已用新 key 实连自检）。
function frAiSaveView(resp, lang) {
  if (!resp || !resp.ok) {
    var extra = resp && resp.detail ? String(resp.detail) : "";
    return { cls: "err", ready: false, text: frT(lang, "save_fail") + (extra ? ": " + extra : "") };
  }
  if (resp.ai_ready) return { cls: "ok", ready: true, text: frT(lang, "ready") };
  return { cls: "warn", ready: false, text: frT(lang, "saved_not_ready") };
}

// 结果步（A5 绿灯）：保存视图 → 终屏标题/副文案
function frResultView(saveView, lang) {
  if (saveView && saveView.ready) {
    return { cls: "ok", title: frT(lang, "result_ok_title"), sub: frT(lang, "result_ok_sub") };
  }
  return { cls: "warn", title: frT(lang, "result_warn_title"), sub: frT(lang, "result_warn_sub") };
}

// 是否弹向导。默认「只弹一次」（有标记就不弹），但支持强制重看：
// 带 `--first-run` 启动即无视标记。有它之前，想再看一遍向导只能让人打开
// DevTools 敲 `localStorage.removeItem('aitr_firstrun_v1')`——这对客服远程
// 协助和自己验收都太别扭（trial_rig.ps1 的复位提示一直就写着这句话）。
// 强制态下**仍然照常写标记**：强制是显式的一次性动作，不该顺手改掉常态。
function frShouldShowWizard(flagPresent, forced) {
  return !!forced || !flagPresent;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FR_STRINGS: FR_STRINGS,
    frT: frT,
    frFormatChars: frFormatChars,
    frShouldShowWizard: frShouldShowWizard,
    frBuildSteps: frBuildSteps,
    frAiPrefill: frAiPrefill,
    frValidateAiInput: frValidateAiInput,
    frAiTestView: frAiTestView,
    frAiSaveView: frAiSaveView,
    frResultView: frResultView,
    frTrialView: frTrialView,
    frClaimValidate: frClaimValidate,
    frClaimResultView: frClaimResultView,
    frClaimPollPlan: frClaimPollPlan,
    frGiftView: frGiftView,
  };
}
